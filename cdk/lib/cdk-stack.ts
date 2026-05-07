import * as path from 'path';
import { Stack, StackProps, RemovalPolicy, Duration } from 'aws-cdk-lib';
import { Construct } from 'constructs';

import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';

import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';

import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as sns from 'aws-cdk-lib/aws-sns';

import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";

export interface IdpPipelineStackProps extends StackProps {
  indexerFn?: lambda.IFunction;
  outputBucket: s3.IBucket;
  enableSearch?: boolean;
  searchMode?: string;
}

export class CdkStack extends Stack {

  public readonly outputBucket: s3.IBucket;

  constructor(scope: Construct, id: string, props: IdpPipelineStackProps) {
    super(scope, id, props);

    const BEDROCK_INFERENCE_PROFILE_ARN = `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/us.amazon.nova-pro-v1:0`;

    /********************************************************************
     * 1) Storage
     ********************************************************************/
    const inputBucket = new s3.Bucket(this, 'InputBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: false,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    this.outputBucket = props.outputBucket;

    const configBucket = new s3.Bucket(this, 'ConfigBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: false,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    this.exportValue(inputBucket.bucketName, { name: 'InputBucket' });
    this.exportValue(this.outputBucket.bucketName, { name: 'OutputBucket' });
    this.exportValue(configBucket.bucketName, { name: 'ConfigBucket' });

    // Deploy schema registry files to config bucket
    new s3deploy.BucketDeployment(this, 'DeploySchemaRegistry', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '../../config/schema_registry'))],
      destinationBucket: configBucket,
      destinationKeyPrefix: 'schema_registry/',
      prune: true,
    });

    /********************************************************************
     * 2) dB Create review table + SNS topic for review notifications
     ********************************************************************/
    const reviewTable = new dynamodb.Table(this, 'ReviewTable', {
      partitionKey: { name: 'reviewId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const reviewTopic = new sns.Topic(this, 'ReviewTopic', {
      displayName: 'IDP Review Notifications',
    });

    // Documents table for extracted data storage + indexing
    const docsTable = new dynamodb.Table(this, 'DocumentsTable', {
      partitionKey: { name: 'docId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY, // dev; switch to RETAIN for prod
    });

    docsTable.addGlobalSecondaryIndex({
      indexName: 'GSI_DocTypeCreatedAt',
      partitionKey: { name: 'docType', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'createdAt', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    docsTable.addGlobalSecondaryIndex({
      indexName: 'GSI_PolicyNumber',
      partitionKey: { name: 'policyNumber', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    /********************************************************************
     * 3) Lambdas (assets paths use __dirname so deploy is stable)
     ********************************************************************/

    // Layer
    const commonLayer = new lambda.LayerVersion(this, "CommonPythonLayer", {
      code: lambda.Code.fromAsset(path.join(__dirname, "../../layers/common")),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12,
      lambda.Runtime.PYTHON_3_11,
      lambda.Runtime.PYTHON_3_10],
      description: "Shared config + utils for IDP lambdas"
    });

    const ingestFn = new lambda.Function(this, 'IngestFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/ingest')),
      layers: [commonLayer],
      timeout: Duration.seconds(30),
      // STATE_MACHINE_ARN added after state machine is created
    });

    const textractStartFn = new lambda.Function(this, 'TextractStartFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/textract_start')),
      layers: [commonLayer],
      timeout: Duration.seconds(30),
    });

    const textractGetFn = new lambda.Function(this, 'TextractGetFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/textract_get')),
      layers: [commonLayer],
      timeout: Duration.seconds(60),
      environment: {
        OUTPUT_BUCKET: this.outputBucket.bucketName,
      },
    });

    const classifyFn = new lambda.Function(this, 'ClassifyFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/classify')),
      layers: [commonLayer],
      timeout: Duration.seconds(60),
      environment: {
        OUTPUT_BUCKET: this.outputBucket.bucketName,
        // IMPORTANT: use your inference profile ARN/ID here
        BEDROCK_MODEL_ID: //process.env.BEDROCK_MODEL_ID ?? 'REPLACE_WITH_INFERENCE_PROFILE_ARN_OR_ID',
          BEDROCK_INFERENCE_PROFILE_ARN,

      },
    });

    const createReviewRequestFn = new lambda.Function(this, 'CreateReviewRequestFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/create_review_request')),
      layers: [commonLayer],
      timeout: Duration.seconds(30),
      environment: {
        REVIEW_TABLE_NAME: reviewTable.tableName,
        REVIEW_TOPIC_ARN: reviewTopic.topicArn, // publish notifications (optional, no subscription required)
      },
    });

    const reviewApiFn = new lambda.Function(this, 'ReviewApiFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/review_api')),
      layers: [commonLayer],
      timeout: Duration.seconds(30),
      environment: {
        REVIEW_TABLE_NAME: reviewTable.tableName,
      },
    });

    const extractFn = new lambda.Function(this, 'ExtractFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/extract')),
      layers: [commonLayer],
      timeout: Duration.seconds(90),
      environment: {
        OUTPUT_BUCKET: this.outputBucket.bucketName,
        BEDROCK_MODEL_ID: BEDROCK_INFERENCE_PROFILE_ARN, // same as classifier
        CONFIG_BUCKET: configBucket.bucketName,
        SCHEMA_PREFIX: "schema_registry",
        SCHEMA_CACHE_TTL_SECONDS: "300",
      },
    });

    const validateSchemaFn = new lambda.Function(this, 'ValidateSchemaFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/validate_schema')),
      layers: [commonLayer],
      timeout: Duration.seconds(90),
      environment: {
        OUTPUT_BUCKET: this.outputBucket.bucketName,
        CONFIG_BUCKET: configBucket.bucketName,
        VALIDATE_SCHEMA_PREFIX: "schemas",
        REPORT_PREFIX: "validation_reports",
        BEDROCK_MODEL_ID: BEDROCK_INFERENCE_PROFILE_ARN,
      },
    });

    const moveToProcessedFn = new lambda.Function(this, 'MoveToProcessed', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/move_to_processed')),
      layers: [commonLayer],
      timeout: Duration.seconds(30),
      environment: {
        INPUT_BUCKET: inputBucket.bucketName,
        SOURCE_PREFIX: 'incoming/',
        DEST_PREFIX: 'processed/',
      },
    });

    const moveToRejectedFn = new lambda.Function(this, 'MoveToRejected', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/move_to_rejected')),
      layers: [commonLayer],
      timeout: Duration.seconds(30),
      environment: {
        INPUT_BUCKET: inputBucket.bucketName,
        SOURCE_PREFIX: 'incoming/',
        DEST_PREFIX: 'rejected/',
      },
    });

    /********************************************************************
     * 4) Permissions
     ********************************************************************/
    // Textract APIs
    const textractPolicy = new iam.PolicyStatement({
      actions: ['textract:StartDocumentAnalysis', 'textract:GetDocumentAnalysis'],
      resources: ['*'],
    });
    textractStartFn.addToRolePolicy(textractPolicy);
    textractGetFn.addToRolePolicy(textractPolicy);

    // Textract must read input object metadata; and Get writes JSON to output bucket
    inputBucket.grantRead(textractStartFn);
    this.outputBucket.grantPut(textractGetFn);

    // Classifier reads Textract JSON + writes classification JSON
    this.outputBucket.grantReadWrite(classifyFn);

    classifyFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: ['*'],
    }));

    // Review request storage + notify
    reviewTable.grantWriteData(createReviewRequestFn);
    reviewTopic.grantPublish(createReviewRequestFn);

    // Review API reads table, writes decisions, resumes Step Functions
    reviewTable.grantReadWriteData(reviewApiFn);
    reviewApiFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['states:SendTaskSuccess', 'states:SendTaskFailure'],
      resources: ['*'],
    }));

    //Extraction function permissions
    this.outputBucket.grantReadWrite(extractFn);
    configBucket.grantRead(extractFn);

    extractFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: ['*'],
    }));

    //Validate schema function permissions
    this.outputBucket.grantReadWrite(validateSchemaFn);
    configBucket.grantRead(validateSchemaFn);

    // Move functions need copy/delete within input bucket
    inputBucket.grantReadWrite(moveToProcessedFn);
    inputBucket.grantReadWrite(moveToRejectedFn);

    /********************************************************************
     * 5) Review API Gateway (dev-friendly; secure later)
     ********************************************************************/
    const api = new apigw.LambdaRestApi(this, 'ReviewApi', {
      handler: reviewApiFn,
      proxy: false,
    });

    api.root.addResource('review').addMethod('POST');

    /********************************************************************
     * 6) Step Functions: Textract -> Classify -> Confidence gate -> Move
     ********************************************************************/
    // Move tasks
    const moveToProcessedAuto = new tasks.LambdaInvoke(this, 'Move to Processed (Auto)', {
      lambdaFunction: moveToProcessedFn,
      payload: sfn.TaskInput.fromObject({
        bucket: sfn.JsonPath.stringAt('$.bucket'),
        key: sfn.JsonPath.stringAt('$.key'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.processed',
    });

    const moveToProcessedAfterReview = new tasks.LambdaInvoke(this, 'Move to Processed (After Review)', {
      lambdaFunction: moveToProcessedFn,
      payload: sfn.TaskInput.fromObject({
        bucket: sfn.JsonPath.stringAt('$.bucket'),
        key: sfn.JsonPath.stringAt('$.key'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.processed',
    });

    const moveToRejectedTask = new tasks.LambdaInvoke(this, 'Move to Rejected', {
      lambdaFunction: moveToRejectedFn,
      payload: sfn.TaskInput.fromObject({
        bucket: sfn.JsonPath.stringAt('$.bucket'),
        key: sfn.JsonPath.stringAt('$.key'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.rejected',
    });

    const moveToRejectedAfterExtraction = new tasks.LambdaInvoke(this, 'Move to Rejected (Post Extraction)', {
      lambdaFunction: moveToRejectedFn,
      payload: sfn.TaskInput.fromObject({
        bucket: sfn.JsonPath.stringAt('$.bucket'),
        key: sfn.JsonPath.stringAt('$.key'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.rejected',
    });

    const moveToRejectedAfterReview = new tasks.LambdaInvoke(this, 'Move to Rejected (Post Review)', {
      lambdaFunction: moveToRejectedFn,
      payload: sfn.TaskInput.fromObject({
        bucket: sfn.JsonPath.stringAt('$.bucket'),
        key: sfn.JsonPath.stringAt('$.key'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.rejected',
    });

    // Textract tasks
    const startTextract = new tasks.LambdaInvoke(this, 'Start Textract', {
      lambdaFunction: textractStartFn,
      outputPath: '$.Payload',
    });

    const getTextractInitial = new tasks.LambdaInvoke(this, 'Get Textract Initial', {
      lambdaFunction: textractGetFn,
      outputPath: '$.Payload',
    });

    const getTextractLoop = new tasks.LambdaInvoke(this, 'Get Textract Loop', {
      lambdaFunction: textractGetFn,
      outputPath: '$.Payload',
    });

    // Classification task
    const classifyTask = new tasks.LambdaInvoke(this, 'Classify Document', {
      lambdaFunction: classifyFn,
      outputPath: '$.Payload',
    });

    //Extraction Task
    const extractTask = new tasks.LambdaInvoke(this, 'Extract Data', {
      lambdaFunction: extractFn,
      payloadResponseOnly: true,
      // Keep the merged extraction output as the main state (it contains source, classification, textractResultKey, extraction.result, etc.)
      // resultPath defaults to '$' which overwrites the state with the merged output.
    });

    const extractAfterReview = new tasks.LambdaInvoke(this, 'Extract Data (After Review)', {
      lambdaFunction: extractFn,
      payloadResponseOnly: true,
      // Same as auto path: keep merged extraction output as the main state.
    });

    // Final states (defined early because they're referenced by catches below)
    const pipelineDone = new sfn.Succeed(this, 'PipelineDone');
    const rejectedDone = new sfn.Succeed(this, 'RejectedDone');

    const validateSchema = new tasks.LambdaInvoke(this, 'validateSchema', {
      lambdaFunction: validateSchemaFn,
      payloadResponseOnly: true,
      // Preserve the merged extraction state; store validation output under $.validation
      resultPath: '$.validation',
    });

    // If schema validation fails, move the PDF to rejected/ and stop the pipeline.
    validateSchema.addCatch(moveToRejectedAfterExtraction.next(rejectedDone), {
      resultPath: '$.validation_error',
    });

    const validateSchemaAfterReview = new tasks.LambdaInvoke(this, 'validateSchemaAfterReview', {
      lambdaFunction: validateSchemaFn,
      payloadResponseOnly: true,
      resultPath: '$.validation',
    });

    validateSchemaAfterReview.addCatch(moveToRejectedAfterReview.next(rejectedDone), {
      resultPath: '$.validation_error',
    });

    //indexing task which uses json/output from extraction  to index into OSS
    // --- Optional indexing ---
    const searchEnabled = props.enableSearch === true && !!props.indexerFn;

    // If search is disabled, we still need a chainable step (Pass) so the SFN graph is valid.
    const skipIndexing = new sfn.Pass(this, 'Skip Indexing', {
      resultPath: '$.searchIndex',
      parameters: {
        skipped: true,
        reason: 'Search/indexing disabled or indexerFn not provided',
      },
    });

    const skipIndexingAfterReview = new sfn.Pass(this, 'Skip Indexing After Review', {
      resultPath: '$.searchIndex',
      parameters: {
        skipped: true,
        reason: 'Search/indexing disabled or indexerFn not provided',
      },
    });

    const indexToSearch = searchEnabled
      ? new tasks.LambdaInvoke(this, 'Index to OpenSearch', {
        lambdaFunction: props.indexerFn!, // safe because searchEnabled implies it exists
        payload: sfn.TaskInput.fromObject({
          pdf_bucket: sfn.JsonPath.stringAt('$.processed.bucket'),
          pdf_key: sfn.JsonPath.stringAt('$.processed.processedKey'),
          'merged_doc.$': '$',
        }),
        payloadResponseOnly: true,
        resultPath: '$.searchIndex',
      })
      : skipIndexing;

    const indexToSearchAfterReview = searchEnabled
      ? new tasks.LambdaInvoke(this, 'Index to OpenSearch After Review', {
        lambdaFunction: props.indexerFn!, // safe because searchEnabled implies it exists
        payload: sfn.TaskInput.fromObject({
          pdf_bucket: sfn.JsonPath.stringAt('$.processed.bucket'),
          pdf_key: sfn.JsonPath.stringAt('$.processed.processedKey'),
          'merged_doc.$': '$',
        }),
        payloadResponseOnly: true,
        resultPath: '$.searchIndex',
      })
      : skipIndexingAfterReview;

    console.log('same?', indexToSearch === indexToSearchAfterReview);

      // Human review callback task (WAIT_FOR_TASK_TOKEN)
    const createReviewRequestTask = new tasks.LambdaInvoke(this, 'Create Review Request', {
      lambdaFunction: createReviewRequestFn,
      integrationPattern: sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
      payload: sfn.TaskInput.fromObject({
        taskToken: sfn.JsonPath.taskToken,
        bucket: sfn.JsonPath.stringAt('$.bucket'),
        key: sfn.JsonPath.stringAt('$.key'),
        textractResultKey: sfn.JsonPath.stringAt('$.textractResultKey'),
        classification: sfn.JsonPath.objectAt('$.classification'),
      }),
      // Put callback output here so we don't overwrite the whole state
      resultPath: '$.humanReview',
    });

    const textractFailed = new sfn.Fail(this, 'TextractFailed', {
      error: 'TextractFailed',
      cause: 'Textract job did not succeed',
    });

    const wait5s = new sfn.Wait(this, 'Wait 5 seconds', {
      time: sfn.WaitTime.duration(Duration.seconds(5)),
    });

    const textractChoice = new sfn.Choice(this, 'Textract Done?');

    // If reviewer rejects, the callback fails -> catch routes to rejected flow
    const markRejected = new sfn.Pass(this, 'MarkRejected', {
      parameters: {
        finalStatus: 'REJECTED',
        'reviewError.$': '$.reviewError',
        'bucket.$': '$.bucket',
        'key.$': '$.key',
        'textractResultKey.$': '$.textractResultKey',
        'classification.$': '$.classification',
      },
    });

    createReviewRequestTask.addCatch(
      moveToRejectedTask.next(markRejected).next(rejectedDone),
      {
        errors: ['HumanRejected'],
        resultPath: '$.reviewError',
      },
    );

    // Apply human-reviewed classification into the main state (since resultPath is $.humanReview)
    const applyHumanClassification = new sfn.Pass(this, 'Apply Human Classification', {
      parameters: {
        'bucket.$': '$.bucket',
        'key.$': '$.key',
        'textractResultKey.$': '$.textractResultKey',
        'classification.$': '$.humanReview.classification',
        'reviewId.$': '$.humanReview.reviewId',
        'reviewStatus.$': '$.humanReview.reviewStatus',
      },
    });

    // Confidence gate
    const confidenceThreshold = 0.90;
    const reviewChoice = new sfn.Choice(this, 'Confidence OK?');

    //Review choice definition
    reviewChoice
      .when(
        sfn.Condition.or(
          sfn.Condition.numberLessThan('$.classification.confidence', confidenceThreshold),
          sfn.Condition.stringEquals('$.classification.doc_type', 'Other'),
        ),
        createReviewRequestTask
          .next(applyHumanClassification)
          .next(extractAfterReview)
          .next(validateSchemaAfterReview)
          .next(moveToProcessedAfterReview)
          .next(indexToSearchAfterReview)
          .next(pipelineDone),
      )
      .otherwise(
        extractTask.next(validateSchema)
          .next(moveToProcessedAuto)
          .next(indexToSearch)
          .next(pipelineDone),
      );

    // Textract loop definition
    textractChoice
      .when(
        sfn.Condition.stringEquals('$.status', 'IN_PROGRESS'),
        wait5s.next(getTextractLoop).next(textractChoice),
      )
      .when(
        sfn.Condition.stringEquals('$.status', 'SUCCEEDED'),
        classifyTask.next(reviewChoice),
      )
      .otherwise(textractFailed);

    const definition = startTextract
      .next(getTextractInitial)
      .next(textractChoice);

    const stateMachine = new sfn.StateMachine(this, 'IdpPipeline', {
      stateMachineName: 'ak-idp-pipeline',
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      timeout: Duration.minutes(15),
    });

    /********************************************************************
     * 7) Wire ingest Lambda to start state machine + S3 trigger
     ********************************************************************/
    ingestFn.addEnvironment('STATE_MACHINE_ARN', stateMachine.stateMachineArn);
    stateMachine.grantStartExecution(ingestFn);

    inputBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED_PUT,
      new s3n.LambdaDestination(ingestFn),
      { prefix: 'incoming/' },
    );
  }
}
