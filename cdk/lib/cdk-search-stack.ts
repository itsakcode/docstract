import * as cdk from 'aws-cdk-lib';
import { Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as oss from 'aws-cdk-lib/aws-opensearchserverless';
import * as path from 'path';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';


interface IdpSearchStackProps extends cdk.StackProps {
    outputBucket: s3.IBucket;
    docsTable?: dynamodb.ITable; // OPTIONAL (only for enrichment in search results)
}


export class IdpSearchStack extends Stack {

    // Expose the indexer function for use in other stacks
    public readonly indexerFn: lambda.Function;

    constructor(scope: Construct, id: string, props: IdpSearchStackProps) {
        super(scope, id, props);

        const stage = this.node.tryGetContext('stage') ?? 'dev';
        const suffix = `${stage}-${Stack.of(this).account}-${Stack.of(this).region}`;
        const collectionName = `idp-search-${suffix}`.slice(0, 32); // keep within limits
        const policyBase = `idp-search-${suffix}`.slice(0, 21);    // 21 + len('-encryption') = 32

        //const collectionName = 'idp-vector';
        const indexName = 'ak-idp-global-chunks';

        const BEDROCK_INFERENCE_PROFILE_ARN = `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/us.amazon.nova-pro-v1:0`;

        // --- Encryption policy (required)
        const encryptionPolicy = new oss.CfnSecurityPolicy(this, 'EncPolicy', {
            name: `${policyBase}-encryption`,
            type: 'encryption',
            policy: JSON.stringify({
                Rules: [{ ResourceType: 'collection', Resource: [`collection/${collectionName}`] }],
                AWSOwnedKey: true,
            }),
        });

        // --- Network policy (public for demo; lock down later)
        const networkPolicy = new oss.CfnSecurityPolicy(this, 'NetPolicy', {
            name: `${policyBase}-network`,
            type: 'network',
            policy: JSON.stringify([
                {
                    Rules: [
                        { ResourceType: 'collection', Resource: [`collection/${collectionName}`] },
                        { ResourceType: 'dashboard', Resource: [`collection/${collectionName}`] },
                    ],
                    AllowFromPublic: true,
                },
            ]),
        });

        // --- Vector collection
        const collection = new oss.CfnCollection(this, 'IDPVectorCollection', {
            name: collectionName,
            type: 'VECTORSEARCH',
            description: 'IDP Global Document Search (hybrid + embeddings)',
        });
        collection.addDependency(encryptionPolicy);
        collection.addDependency(networkPolicy);


        new ssm.StringParameter(this, 'OpenSearchEndpointParam', {
            parameterName: `/idp/${stage}/opensearch/endpoint`,
            stringValue: collection.attrCollectionEndpoint,
        });

        new ssm.StringParameter(this, 'OpenSearchCollectionIdParam', {
            parameterName: `/idp/${stage}/opensearch/collectionId`,
            stringValue: collection.attrId,
        });

        // --- Roles
        const indexerRole = new iam.Role(this, 'IndexerRole', {
            assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        });
        indexerRole.addManagedPolicy(
            iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')
        );

        const searchRole = new iam.Role(this, 'SearchRole', {
            assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        });
        searchRole.addManagedPolicy(
            iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')
        );

        searchRole.addToPolicy(new iam.PolicyStatement({
            actions: [
                'bedrock-runtime:InvokeModel',
                'bedrock-runtime:Converse',
                // optional if you later stream:
                // 'bedrock-runtime:ConverseStream',
                // 'bedrock-runtime:InvokeModelWithResponseStream',
            ],
            resources: ['*'], // demo-friendly; tighten later to model/inference profile ARNs
        }));


        // Bedrock invoke (embeddings)
        for (const role of [indexerRole, searchRole]) {
            role.addToPolicy(
                new iam.PolicyStatement({
                    actions: ['bedrock:InvokeModel'],
                    resources: ['*'],
                })
            );
        }

        for (const role of [indexerRole, searchRole]) {
            role.addToPolicy(
                new iam.PolicyStatement({
                    actions: ['aoss:APIAccessAll'],
                    resources: ['*'],
                })
            );
        }

        // S3 read for indexer (you’ll scope to your buckets in pipeline stack)
        indexerRole.addToPolicy(
            new iam.PolicyStatement({
                actions: ['s3:GetObject'],
                resources: ['*'],
            })
        );

        // --- Data access policy (include BOTH roles)
        const accessPolicy = new oss.CfnAccessPolicy(this, 'DataAccessPolicy', {
            name: `${policyBase}-data`,
            type: 'data',
            policy: JSON.stringify([
                {
                    Rules: [
                        {
                            ResourceType: 'collection',
                            Resource: [`collection/${collectionName}`],
                            Permission: ['aoss:DescribeCollectionItems', 'aoss:CreateCollectionItems'],
                        },
                        {
                            ResourceType: 'index',
                            Resource: [`index/${collectionName}/*`],
                            Permission: ['aoss:*'],
                        },
                    ],
                    Principal: [indexerRole.roleArn, searchRole.roleArn],
                },
            ]),
        });
        accessPolicy.addDependency(collection);
        accessPolicy.addDependency(encryptionPolicy);
        accessPolicy.addDependency(networkPolicy);


        // --- Lambdas (paths are placeholders)
        const openSearchLayer = new lambda.LayerVersion(this, 'OpenSearchLayer', {
            code: lambda.Code.fromAsset(path.join(__dirname, '../../layers/opensearch')),
            compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
            description: 'OpenSearch Python client',
        });

        this.indexerFn = new lambda.Function(this, 'IndexerFn', {
            runtime: lambda.Runtime.PYTHON_3_12,
            handler: 'handler.lambda_handler',
            code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/search_indexer')),
            role: indexerRole,
            timeout: cdk.Duration.seconds(60),
            memorySize: 1024,
            layers: [openSearchLayer],
            environment: {
                AOSS_ENDPOINT: collection.attrCollectionEndpoint, // e.g. https://xxxx.us-east-2.aoss.amazonaws.com
                AOSS_COLLECTION: collectionName,
                INDEX_NAME: indexName,
                EMBED_MODEL_ID: 'amazon.titan-embed-text-v2:0',
                EMBED_DIM: '1024',
                TEXTRACT_BUCKET: props.outputBucket.bucketName,
            },
        });

        //permissions for output bucket
        props.outputBucket.grantRead(this.indexerFn);

        const searchFn = new lambda.Function(this, 'SearchFn', {
            runtime: lambda.Runtime.PYTHON_3_12,
            handler: 'handler.lambda_handler',
            code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/search_api')),
            role: searchRole,
            timeout: cdk.Duration.seconds(30),
            memorySize: 1024,
            layers: [openSearchLayer],
            environment: {
                AOSS_ENDPOINT: collection.attrCollectionEndpoint,
                INDEX_NAME: indexName,
                EMBED_MODEL_ID: 'amazon.titan-embed-text-v2:0',
                EMBED_DIM: '1024',

                // Optional enrichment
                ...(props.docsTable ? { DOCS_TABLE: props.docsTable.tableName, DOCS_PK: 'doc_id' } : {}),
            },
        });

        // If docsTable provided, allow reads from it
        if (props.docsTable) {
            props.docsTable.grantReadData(searchFn);
        }

        // --- QA Lambda
        const qaFn = new lambda.Function(this, 'QaFn', {
            runtime: lambda.Runtime.PYTHON_3_12,
            handler: 'handler.lambda_handler',
            code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/qa_api')),
            role: searchRole,
            timeout: cdk.Duration.seconds(60),
            memorySize: 1536,
            layers: [openSearchLayer],
            environment: {
                AOSS_ENDPOINT: collection.attrCollectionEndpoint,
                INDEX_NAME: indexName,
                EMBED_MODEL_ID: 'amazon.titan-embed-text-v2:0',
                EMBED_DIM: '1024',
                // Pick what you’re using for generation in Bedrock:
                // e.g. 'amazon.nova-lite-v1:0' or a Claude model id you have access to
                QA_MODEL_ID: BEDROCK_INFERENCE_PROFILE_ARN,
            },
        });

        // --- Router Lambda (single public Function URL)
        const apiRouterFn = new lambda.Function(this, 'ApiRouterFn', {
            runtime: lambda.Runtime.NODEJS_20_X,
            handler: 'handler.handler',
            code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/api_router')),
            timeout: cdk.Duration.seconds(30),
            memorySize: 512,
            environment: {
                SEARCH_FN_ARN: searchFn.functionArn,
                QA_FN_ARN: qaFn.functionArn,
            },
            logRetention: logs.RetentionDays.ONE_WEEK,
        });

        // Router needs permission to invoke the internal lambdas
        searchFn.grantInvoke(apiRouterFn);
        qaFn.grantInvoke(apiRouterFn);

        apiRouterFn.addToRolePolicy(new iam.PolicyStatement({
            actions: ['lambda:InvokeFunction'],
            resources: [searchFn.functionArn, qaFn.functionArn],
        }));

        // Function URL for router
        const apiUrl = apiRouterFn.addFunctionUrl({
            authType: lambda.FunctionUrlAuthType.NONE, // demo-friendly; lock down later
            cors: {
                allowedOrigins: ['*'],
                allowedMethods: [lambda.HttpMethod.ALL],
                allowedHeaders: ['*'],
            },
        });

        new cdk.CfnOutput(this, 'QaFnName', { value: qaFn.functionName });
        new cdk.CfnOutput(this, 'ApiBaseUrl', { value: apiUrl.url });
        new cdk.CfnOutput(this, 'AossEndpoint', { value: collection.attrCollectionEndpoint });
        new cdk.CfnOutput(this, 'IndexerFnName', { value: this.indexerFn.functionName });
        new cdk.CfnOutput(this, 'SearchFnName', { value: searchFn.functionName });
    }
}
