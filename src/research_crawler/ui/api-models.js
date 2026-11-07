'use strict';
(() => {
  const section=node('section',undefined,'account-section'),details=node('details');
  details.id='model-api-settings';details.append(node('summary','模型 API 配置'));section.append(details);$('history').before(section);
  details.append(node('p','配置只保存在本机；Windows 使用当前账号加密保护，其他系统使用仅所有者可读的文件。保存不调用模型。选择 API 回答后，已采集资料会发送给所选服务，并可能按服务商规则计费。','account-help'));
  const form=node('form'),fields=node('div',undefined,'field-row');
  function field(parent,label,element){const wrap=node('label',label);wrap.append(element);parent.append(wrap);return element;}
  const name=field(fields,'配置名称',node('input'));name.id='model-api-name';name.required=true;name.pattern='[A-Za-z0-9][A-Za-z0-9_.\\-]{0,99}';
  const provider=field(fields,'提供方',node('select'));provider.id='model-api-provider';provider.add(new Option('OpenAI 兼容','openai'));provider.add(new Option('Anthropic','anthropic'));
  const endpoint=field(fields,'服务地址',node('input'));endpoint.id='model-api-base-url';endpoint.type='url';endpoint.required=true;endpoint.placeholder='https://api.example.com/v1';
  const model=field(fields,'模型名称',node('input'));model.id='model-api-model';model.required=true;
  const key=field(fields,'API 密钥',node('input'));key.id='model-api-key';key.type='password';key.autocomplete='new-password';key.required=true;
  const save=node('button','保存配置');save.type='submit';form.append(fields,save);
  const status=node('p','','account-help');status.id='model-api-status';status.setAttribute('role','status');
  const list=node('div');list.id='model-api-profiles';details.append(form,status,list);
  const selection=node('div',undefined,'field-row');selection.id='api-profile-option';selection.hidden=true;
  const choice=field(selection,'模型 API',node('select'));choice.id='api-profile';
  const disclosure=node('p','本次回答会将已采集资料发送给所选 API 服务。','account-help');selection.append(disclosure);
  $('answer-backend-option').after(selection);

  window.syncApiModelSelection=()=>{
    const selectedAPI=['model','research'].includes($('mode').value)&&$('answer-backend').value==='api';
    selection.hidden=!selectedAPI;choice.required=selectedAPI;choice.disabled=!selectedAPI||!apiModelProfiles.length;
  };
  window.renderApiModelProfiles=profiles=>{
    apiModelProfiles=profiles;answerAvailability.api=profiles.length>0;
    const previous=choice.value;choice.replaceChildren();
    if(!profiles.length)choice.add(new Option('先在下方保存配置',''));
    for(const profile of profiles)choice.add(new Option(profile.name+' · '+profile.model,profile.name));
    if(profiles.some(p=>p.name===previous))choice.value=previous;
    const option=[...$('answer-backend').options].find(o=>o.value==='api');option.disabled=!profiles.length;
    list.replaceChildren();
    for(const profile of profiles){
      const row=node('div',undefined,'account-row'),edit=node('button','更改配置'),remove=node('button','删除');edit.type=remove.type='button';
      row.append(node('span',profile.name+' · '+profile.provider+' · '+profile.model),edit,remove);
      edit.addEventListener('click',()=>{name.value=profile.name;provider.value=profile.provider;endpoint.value=profile.base_url;model.value=profile.model;key.value='';key.focus();status.textContent='修改后请重新填写密钥；保存会替换同名配置。';});
      remove.addEventListener('click',async()=>{remove.disabled=true;try{await api('model-profiles/'+encodeURIComponent(profile.name),{method:'DELETE'});await reload();status.textContent='已删除本机配置。';}catch(error){status.textContent=error.message;remove.disabled=false;}});
      list.append(row);
    }
    window.syncApiModelSelection();
  };
  async function reload(){const result=await api('model-profiles');window.renderApiModelProfiles(result.profiles);updateMode();}
  form.addEventListener('submit',async event=>{
    event.preventDefault();save.disabled=true;
    try{
      await api('model-profiles/'+encodeURIComponent(name.value.trim()),{method:'PUT',body:JSON.stringify({provider:provider.value,base_url:endpoint.value.trim(),model:model.value.trim(),api_key:key.value})});
      key.value='';await reload();status.textContent='已保存到本机受保护配置。未发送模型请求；请在回答方式中自主选择 API。';
    }catch(error){status.textContent=error.message;}finally{save.disabled=false;}
  });
  window.renderApiModelProfiles(apiModelProfiles);
})();
