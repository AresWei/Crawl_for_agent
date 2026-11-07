'use strict';
(() => {
  let pools=[],savedAccounts=[],proxies=[],revision=null;
  const section=node('section',undefined,'account-section'),details=node('details');
  details.append(node('summary','账号与代理池'));section.append(details);$('history').before(section);
  const status=node('p','只使用已登录账号和你提供的代理。','account-help');status.setAttribute('role','status');
  const poolForm=node('form'),fields=node('div',undefined,'field-row');
  function field(parent,label,element){const wrap=node('label',label);wrap.append(element);parent.append(wrap);return element;}
  const name=field(fields,'池名称',node('input'));name.required=true;name.pattern='[A-Za-z0-9][A-Za-z0-9_.\\-]{0,99}';
  const platform=field(fields,'平台',node('select'));
  for(const [code,label] of [['xhs','小红书'],['dy','抖音'],['ks','快手'],['bili','哔哩哔哩'],['wb','微博'],['tieba','贴吧'],['zhihu','知乎']])platform.add(new Option(label,code));
  const rows=node('div'),add=node('button','添加账号'),save=node('button','保存池');add.type='button';save.type='submit';
  const buttons=node('div',undefined,'field-row');buttons.append(add,save);poolForm.append(fields,rows,buttons);
  const list=node('div');details.append(poolForm,status,list);
  function addRow(member={}){
    const row=node('div',undefined,'field-row'),account=field(row,'账号',node('select')),proxy=field(row,'固定代理',node('select'));
    account.required=true;proxy.required=true;
    account.add(new Option('选择已登录账号',''));proxy.add(new Option('选择代理',''));
    for(const a of savedAccounts.filter(a=>a.platform===platform.value&&a.cookie_count>0&&a.status!=='revoked'))account.add(new Option(a.account,a.account));
    for(const p of proxies)proxy.add(new Option(p.name,p.name));
    account.value=member.account||'';proxy.value=member.proxy_id||'';
    const remove=node('button','移除');remove.type='button';remove.addEventListener('click',()=>row.remove());row.append(remove);rows.append(row);
  }
  const session=$('session-option'),poolChoice=field(session,'账号池',node('select'));poolChoice.id='account-pool-select';
  const signing=field(session,'签名方式',node('select'));signing.id='signing-backend';signing.add(new Option('本机直接签名','local'));signing.add(new Option('独立本机签名服务','service'));
  function currentPlatform(){const mode=$('mode').value;return Object.hasOwn(platformCaps,mode)?mode:mode.split('-')[0];}
  function sync(){
    const p=currentPlatform(),eligible=['xhs','dy','ks','bili','wb','tieba','zhihu'].includes(p),old=poolChoice.value;
    poolChoice.replaceChildren(new Option('使用单个账号',''));
    for(const pool of pools.filter(x=>x.platform===p))poolChoice.add(new Option(pool.name,pool.name));
    if([...poolChoice.options].some(o=>o.value===old))poolChoice.value=old;
    poolChoice.parentElement.hidden=!eligible;signing.parentElement.hidden=!['xhs','dy','zhihu'].includes(p);
    $('account-select').disabled=eligible&&!!poolChoice.value;
  }
  async function load(){
    const [a,p,s]=await Promise.all([api('accounts'),api('proxies'),api('account-pools')]);
    savedAccounts=a.accounts;proxies=p.proxies;pools=s.pools;list.replaceChildren();
    for(const pool of pools){
      const block=node('div',undefined,'pool-card'),heading=node('div',undefined,'account-row'),edit=node('button','编辑');edit.type='button';
      heading.append(node('strong',pool.name),node('span',pool.platform),edit);block.append(heading);
      edit.addEventListener('click',()=>{name.value=pool.name;platform.value=pool.platform;revision=pool.revision;rows.replaceChildren();pool.members.forEach(addRow);name.focus();});
      for(const member of pool.members){
        const row=node('div',undefined,'account-row'),toggle=node('button',member.state==='enabled'?'暂停':'启用');toggle.type='button';
        row.append(node('span',member.account+' → '+member.proxy_id),node('span',({enabled:'可分配',paused:'已暂停',attention:'需要处理'})[member.state]||member.state),toggle);block.append(row);
        toggle.addEventListener('click',async()=>{toggle.disabled=true;try{await api(`account-pools/${encodeURIComponent(pool.name)}/members/${encodeURIComponent(member.account)}`,{method:'PATCH',body:JSON.stringify({state:member.state==='enabled'?'paused':'enabled',expected_revision:pool.revision})});await load();status.textContent='已更新；运行中的任务保留原绑定。';}catch(e){status.textContent=e.message;toggle.disabled=false;}});
      }list.append(block);
    }sync();
  }
  poolForm.addEventListener('submit',async event=>{
    event.preventDefault();save.disabled=true;
    try{
      const members=[...rows.children].map(row=>{const s=row.querySelectorAll('select');return {account:s[0].value,proxy_id:s[1].value};});
      if(!members.length)throw new Error('至少添加一个账号与代理绑定。');
      await api('account-pools/'+encodeURIComponent(name.value.trim()),{method:'PUT',body:JSON.stringify({platform:platform.value,members,expected_revision:revision})});
      revision=null;name.value='';rows.replaceChildren();await load();addRow();status.textContent='账号池已保存。';
    }catch(e){status.textContent=e.message;}finally{save.disabled=false;}
  });
  add.addEventListener('click',()=>{if(rows.children.length<32)addRow();});
  platform.addEventListener('change',()=>{revision=null;name.value='';rows.replaceChildren();addRow();});
  name.addEventListener('input',()=>{revision=pools.find(p=>p.name===name.value.trim())?.revision||null;});
  const proxyDetails=node('details');proxyDetails.append(node('summary','登记代理'));
  const proxyForm=node('form',undefined,'field-row'),proxyName=field(proxyForm,'代理别名',node('input')),proxyURL=field(proxyForm,'连接地址',node('input'));
  proxyName.required=proxyURL.required=true;proxyURL.type='password';proxyURL.autocomplete='off';proxyURL.placeholder='http://… 或 socks5://…';
  const proxySave=node('button','保存代理');proxySave.type='submit';proxyForm.append(proxySave);proxyDetails.append(proxyForm);details.append(proxyDetails);
  proxyForm.addEventListener('submit',async event=>{event.preventDefault();proxySave.disabled=true;try{await api('proxies',{method:'POST',body:JSON.stringify({name:proxyName.value.trim(),url:proxyURL.value.trim()})});proxyURL.value='';await load();rows.replaceChildren();addRow();status.textContent='代理已保存到本机受保护配置。';}catch(e){status.textContent=e.message;}finally{proxySave.disabled=false;}});
  details.addEventListener('toggle',()=>{if(details.open)load().then(()=>{if(!rows.children.length)addRow();}).catch(e=>{status.textContent=e.message;});});
  $('mode').addEventListener('change',sync);poolChoice.addEventListener('change',sync);
  window.researchPlatformOptions=body=>{
    if(!['platform.collect','platform.discover'].includes(body.kind))return;
    if(poolChoice.value){delete body.params.account;body.params.account_pool=poolChoice.value;}
    if(['xhs','dy','zhihu'].includes(body.params.platform))body.params.signing_backend=signing.value;
  };
  load().catch(e=>{status.textContent=e.message;});
})();
