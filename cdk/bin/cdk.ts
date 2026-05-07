#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { SharedInfraStack } from '../lib/cdk-shared-infra-stack';
import { CdkStack } from '../lib/cdk-stack';
import { IdpSearchStack } from '../lib/cdk-search-stack';

const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION,
};

// 1) Shared infra first
const infra = new SharedInfraStack(app, 'SharedInfraStack', { env });

// 2) Search stack can now receive output bucket
const enableSearch = app.node.tryGetContext('enableSearch') === 'true';

const searchMode = app.node.tryGetContext('searchMode') ?? 'auto';

let searchStack: IdpSearchStack | undefined = undefined;
if (enableSearch) {
  searchStack = new IdpSearchStack(app, 'IdpSearchStack', {
    env,
    outputBucket: infra.outputBucket,
  });
}

// 3) Pipeline stack receives indexer lambda (no cycle)
new CdkStack(app, 'CdkStack', {
  env,
  outputBucket: infra.outputBucket,
  indexerFn: searchStack?.indexerFn,
  enableSearch,
  searchMode,
});
