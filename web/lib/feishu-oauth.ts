export const FEISHU_AUTHORIZE_URL =
  'https://accounts.feishu.cn/open-apis/authen/v1/authorize';

export function buildFeishuAuthorizeUrl({
  appId,
  redirectUri,
  state,
}: {
  appId: string;
  redirectUri: string;
  state: string;
}): string {
  const params = new URLSearchParams({
    app_id: appId,
    redirect_uri: redirectUri,
    response_type: 'code',
    state,
  });
  return `${FEISHU_AUTHORIZE_URL}?${params.toString()}`;
}
