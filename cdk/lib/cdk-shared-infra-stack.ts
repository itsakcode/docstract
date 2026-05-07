import * as cdk from 'aws-cdk-lib';
import { RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';

export class SharedInfraStack extends Stack {
    public readonly outputBucket: s3.Bucket;

    constructor(scope: Construct, id: string, props?: StackProps) {
        super(scope, id, props);

        this.outputBucket = new s3.Bucket(this, 'OutputBucket', {
            encryption: s3.BucketEncryption.S3_MANAGED,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            versioned: false,
            removalPolicy: RemovalPolicy.RETAIN,
        });
    }
}
