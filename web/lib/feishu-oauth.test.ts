import assert from 'node:assert/strict';
import test from 'node:test';

const { buildFeishuAuthorizeUrl } = await import('./feishu-oauth.ts');

test('builds a Feishu OAuth authorize URL that requests an auth code', () => {
  const url = new URL(buildFeishuAuthorizeUrl({
    appId: 'cli_test',
    redirectUri: 'https://pmo-agent-sigma.vercel.app/api/feishu/oauth/callback',
    state: 'state-1',
  }));

  assert.equal(url.origin + url.pathname, 'https://accounts.feishu.cn/open-apis/authen/v1/authorize');
  assert.equal(url.searchParams.get('app_id'), 'cli_test');
  assert.equal(
    url.searchParams.get('redirect_uri'),
    'https://pmo-agent-sigma.vercel.app/api/feishu/oauth/callback',
  );
  assert.equal(url.searchParams.get('response_type'), 'code');
  assert.equal(url.searchParams.get('state'), 'state-1');
});
