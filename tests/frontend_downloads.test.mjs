import test from 'node:test';
import assert from 'node:assert/strict';
import { downloadFile } from '../app/static/downloads.js';
import { isSessionChange, SessionChangedError } from '../app/static/auth.js';

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((done,fail)=>{resolve=done;reject=fail;});
  return {promise,resolve,reject};
};
const file = () => ({blob:new Blob(['%PDF fixture'],{type:'application/pdf'}),filename:'复习资料-v2.pdf'});

function surface() {
  const state = {appended:[],clicked:[],removed:[],created:[],revoked:[]};
  const document = {
    createElement(tag) {
      assert.equal(tag,'a');
      return {
        click(){state.clicked.push({href:this.href,filename:this.download});},
        remove(){state.removed.push(this);},
      };
    },
    body:{append(link){state.appended.push(link);}},
  };
  const url = {
    createObjectURL(blob){state.created.push(blob);return 'blob:isolated-download';},
    revokeObjectURL(href){state.revoked.push(href);},
  };
  return {document,url,state};
}

test('a document download preserves its supplied filename and releases the attachment URL', async context => {
  context.mock.timers.enable({apis:['setTimeout']});
  const ui=surface(), result=file();
  let request;
  const controller=new AbortController();
  const success=await downloadFile(async(...args)=>{request=args;return result;},'/contents/own/export?format=pdf',{
    ...ui,signal:controller.signal,
  });
  assert.equal(success,true);
  assert.equal(request[0],'/contents/own/export?format=pdf');
  assert.equal(request[1],'GET');
  assert.equal(request[3].responseType,'blob');
  assert.equal(request[3].signal,controller.signal);
  assert.deepEqual(ui.state.created,[result.blob]);
  assert.deepEqual(ui.state.clicked,[{href:'blob:isolated-download',filename:'复习资料-v2.pdf'}]);
  assert.equal(ui.state.removed.length,1);
  context.mock.timers.tick(60001);
  assert.deepEqual(ui.state.revoked,['blob:isolated-download']);
});

test('a cancellation made before export starts performs no request or download', async () => {
  const controller=new AbortController(), ui=surface();
  controller.abort();
  let calls=0;
  const result=await downloadFile(async()=>{calls++;return file();},'/contents/own/export',{
    ...ui,signal:controller.signal,
  });
  assert.equal(result,false);
  assert.equal(calls,0);
  assert.equal(ui.state.created.length,0);
  assert.equal(ui.state.appended.length,0);
  assert.equal(ui.state.clicked.length,0);
});

test('canceling a pending transfer stops it without showing a failure or creating an attachment', async () => {
  const controller=new AbortController(), ui=surface(), started=deferred();
  const pending=downloadFile((_path,_method,_data,{signal})=>{
    started.resolve();
    return new Promise((_,reject)=>signal.addEventListener('abort',()=>{
      reject(new DOMException('The user canceled this transfer.','AbortError'));
    },{once:true}));
  },'/contents/own/export',{...ui,signal:controller.signal});
  await started.promise;
  controller.abort();
  assert.equal(await pending,false);
  assert.equal(ui.state.created.length,0);
  assert.equal(ui.state.clicked.length,0);
});

test('a late response from a transport that ignores cancellation cannot start a download', async () => {
  const controller=new AbortController(), ui=surface(), response=deferred();
  const pending=downloadFile(()=>response.promise,'/contents/own/export',{...ui,signal:controller.signal});
  controller.abort();
  response.resolve(file());
  assert.equal(await pending,false);
  assert.equal(ui.state.created.length,0);
  assert.equal(ui.state.clicked.length,0);
});

test('an interrupted body without user cancellation remains an actionable transfer error', async () => {
  const ui=surface(), controller=new AbortController();
  const pending=downloadFile(async()=>{throw new DOMException('Body interrupted','AbortError');},'/contents/own/export',{
    ...ui,signal:controller.signal,
  });
  await assert.rejects(pending,error=>{
    assert.equal(isSessionChange(error),false);
    assert.ok([error.message,error.uiMessage?.key].includes('下载中断，请重试。'));
    return true;
  });
  assert.equal(controller.signal.aborted,false);
  assert.equal(ui.state.created.length,0);
  assert.equal(ui.state.clicked.length,0);
});

test('account invalidation stays a session error and never exports the old account file', async () => {
  const ui=surface();
  await assert.rejects(downloadFile(async()=>{throw new SessionChangedError();},'/contents/own/export',ui),isSessionChange);
  assert.equal(ui.state.created.length,0);
  assert.equal(ui.state.clicked.length,0);
});

test('a failed transfer can be retried and a server without filename uses the caller fallback', async context => {
  context.mock.timers.enable({apis:['setTimeout']});
  const ui=surface();
  let calls=0;
  const api=async()=>{
    calls++;
    if(calls===1)throw new TypeError('Network connection lost');
    return {...file(),filename:''};
  };
  await assert.rejects(downloadFile(api,'/contents/own/export',ui));
  assert.equal(ui.state.clicked.length,0);
  assert.equal(await downloadFile(api,'/contents/own/export',{...ui,filename:'lesson.pdf'}),true);
  assert.equal(calls,2);
  assert.equal(ui.state.clicked.length,1);
  assert.equal(ui.state.clicked[0].filename,'lesson.pdf');
});
