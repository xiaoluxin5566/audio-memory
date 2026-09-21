const ASR_ERROR_MESSAGES = {
  invalid_api_key: 'API Key 无效，请重新填写。',
  permission_denied: '豆包暂时无法使用该资源：请在控制台检查录音文件识别 2.0 是否已开通、资源包是否用完，以及账户是否欠费。',
  rate_limited: '豆包当前请求过多，请稍后重试。',
  provider_unavailable: '豆包服务暂时不可用，请稍后重试。',
  timeout: '豆包连接超时，请检查网络后重试。',
  network_error: '无法连接豆包，请检查网络后重试。',
};

export function asrErrorMessage(failure, fallback) {
  return ASR_ERROR_MESSAGES[failure?.code] || failure?.message || fallback;
}
