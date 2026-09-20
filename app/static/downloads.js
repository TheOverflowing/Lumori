// Generated documents use the account-aware client; stored media streams natively.
export async function downloadFile(api, path, {filename = 'lumori', signal, document:doc = document, url:urls = URL} = {}) {
  if(signal?.aborted)return false;
  let result;
  try {
    result = await api(path, 'GET', undefined, {responseType:'blob',signal});
  } catch(error) {
    if(signal?.aborted)return false;
    if(error.name==='AbortError')throw new Error('下载中断，请重试。');
    throw error;
  }
  if(signal?.aborted)return false;
  const href = urls.createObjectURL(result.blob), link = doc.createElement('a');
  link.href = href;
  link.download = result.filename || filename;
  link.hidden = true;
  doc.body.append(link);
  try { link.click(); }
  finally { link.remove(); setTimeout(() => urls.revokeObjectURL(href), 60000); }
  return true;
}
