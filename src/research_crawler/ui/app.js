'use strict';
const token = document.querySelector('meta[name="research-token"]').content;
const $ = id => document.getElementById(id);
const states = {pending:'等待执行',running:'进行中',complete:'已完成',partial:'部分完成',auth_required:'需要登录',failed:'执行失败',paused:'已暂停',cancelled:'已取消',interrupted:'执行中断'};
let selected = null, polling = false, platformCaps = {}, accounts = [], handlers = [], exportTargets = [], renderedJobSignature = null;
let answerAvailability = {local:false,codex:false};
const homepageModes={
  'bili-trending':{platform:'bili',mode:'trending',label:'哔哩哔哩热搜'},
  'bili-homefeed':{platform:'bili',mode:'homefeed',label:'哔哩哔哩首页推荐'},
  'xhs-homefeed':{platform:'xhs',mode:'homefeed',label:'小红书首页推荐'}
};
async function api(path, options={}) {
  const response = await fetch('/api/'+path, {...options, headers:{'X-Research-Token':token,'Content-Type':'application/json',...(options.headers||{})}});
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail));
  return body;
}
function node(tag,text,className){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(className)e.className=className;return e;}
function message(text,error=false){$('form-status').textContent=text;$('form-status').classList.toggle('error-text',error);}
function answerBackendParams(mode){
  const backend=$('answer-backend').value;
  return ['model','research'].includes(mode)?{answer_backend:backend,...(mode==='research'?{evidence_selection:backend==='codex'?'context':'planned'}:{})}:{};
}
function imageCreationParams(mode){
  const params={prompt:$('urls').value.trim(),seed:Number($('image-seed').value),max_seconds:600};
  const instruction=mode==='image-edit'&&$('image-edit-method').value==='instruction';
  if(instruction)Object.assign(params,{model_profile:'instruct_pix2pix',edit_mode:'instruction',image_cfg_scale:1.0,cfg_scale:7.5,steps:30,width:512,height:512,iterations:1,negative_prompt:'blurry, distorted, text, watermark'});
  else if($('image-template').value)params.template=$('image-template').value;
  if(mode==='image-edit'){
    const reference=$('image-reference');params.reference_path=reference.value.trim();
    if(reference.dataset.sha256&&reference.dataset.sourcePath===params.reference_path)params.reference_sha256=reference.dataset.sha256;
  }
  return params;
}
function clearImageReferenceHash(){
  delete $('image-reference').dataset.sha256;delete $('image-reference').dataset.sourcePath;
}
function prepareGeneratedImageEdit(file){
  if(file.role!=='generated_image'||!file.path||!/^[a-f0-9]{64}$/i.test(file.sha256||''))return false;
  $('mode').value='image-edit';$('image-edit-method').value='instruction';
  const reference=$('image-reference');reference.value=file.path;reference.dataset.sha256=file.sha256;reference.dataset.sourcePath=file.path;
  $('urls').value='';updateMode();$('urls').focus({preventScroll:true});
  $('collect-form').scrollIntoView({block:'start',behavior:'instant'});return true;
}
function isPlatform(){return Object.hasOwn(platformCaps,$('mode').value);}
function updateAccounts(){
  const previous=$('account-select').value;
  $('account-select').replaceChildren(new Option('匿名访问',''));
  const platform=homepageModes[$('mode').value]?.platform||(isPlatform()?$('mode').value:$('mode').value==='bili-hot'?'bili':$('mode').value==='tieba-hot'?'tieba':$('mode').value.startsWith('reddit-')?'reddit':'web');
  for(const account of accounts.filter(a=>a.platform===platform && a.status!=='revoked')) $('account-select').add(new Option(account.account,account.account));
  if([...$('account-select').options].some(o=>o.value===previous))$('account-select').value=previous;
}
function updateMode(){
  const mode=$('mode').value, platform=isPlatform(), local=['search','semantic','answer','model','research','web-search'].includes(mode);
  const fileMode=['ocr','transcribe','vision'].includes(mode), creation=['image-generate','image-edit'].includes(mode);
  const discovery=['seed','map'].includes(mode);
  const platformDiscovery=Object.hasOwn(homepageModes,mode)||['bili-hot','tieba-hot','reddit-hot','reddit-qa','qa-search','qa-detail'].includes(mode);
  const qa=['qa-search','qa-detail'].includes(mode);
  $('qa-options').hidden=!qa;
  $('extraction-options').hidden=mode!=='web-extract';
  $('web-options').hidden=!['web','web-extract','web-semantic'].includes(mode);
  $('web-ua-value').required=['web','web-extract','web-semantic'].includes(mode)&&$('web-ua-mode').value==='explicit';
  $('discovery-options').hidden=!discovery;
  for(const source of ['wayback','crt'])$('discovery-'+source).disabled=mode!=='map';
  $('retrieval-option').hidden=mode!=='model';
  $('answer-backend-option').hidden=!['model','research'].includes(mode);
  $('image-options').hidden=!creation;
  $('image-reference').parentElement.hidden=mode!=='image-edit';
  $('image-reference').required=mode==='image-edit';
  $('image-edit-method').parentElement.hidden=mode!=='image-edit';
  $('image-template').parentElement.hidden=mode==='image-edit'&&$('image-edit-method').value==='instruction';
  $('urls').required=!Object.hasOwn(homepageModes,mode)&&!['bili-hot','tieba-hot'].includes(mode);
  const noSourceInput=Object.hasOwn(homepageModes,mode)||['bili-hot','tieba-hot'].includes(mode);
  $('urls').hidden=noSourceInput;document.querySelector('label[for="urls"]').hidden=noSourceInput;
  $('platform-options').hidden=!platform;$('session-option').hidden=local||fileMode||discovery||creation||qa;
  $('pages-option').hidden=local||mode==='media'||fileMode||creation;$('comments-option').hidden=!platform;
  const operation=$('platform-mode').value;
  $('urls').placeholder=discovery?'输入一个网站或站点地图链接':fileMode?'F:\\…\n每行一个完整文件路径':local?'输入问题或要查找的词句':platform?(operation==='search'?'搜索词，每行一个':operation==='creator'?'创作者主页链接或 ID，每行一个':'帖子链接或 ID，每行一个'):'https://…\n每行一个链接';
  document.querySelector('label[for="urls"]').textContent=fileMode?'本机文件路径':local?'查找内容':platform&&operation==='search'?'搜索词':'来源链接';
  if(qa)document.querySelector('label[for="urls"]').textContent=mode==='qa-search'?'搜索问题':'问题 ID';
  updateAccounts();
  if(creation){const instruction=mode==='image-edit'&&$('image-edit-method').value==='instruction';$('urls').placeholder=instruction?'描述这一步需要修改的内容':'描述画面主体、场景与风格';document.querySelector('label[for="urls"]').textContent=instruction?'编辑指令':'画面描述';message(instruction?'输入新指令后开始编辑；原图保留。':'本机生成 512 × 512 图片；图像与语言模型按需排队使用显卡。');return;}
  if(mode==='vision'){message('分析图片与视频采样帧；保留帧时间，模型描述需要核对。');return;}
  if(mode==='semantic'){message('按语义检索本地资料，保留原文和实际读取范围。');return;}
  if(mode==='web-semantic'){message('读取网页后在本机分组；语义分组属于模型判断。');return;}
  if(Object.hasOwn(homepageModes,mode)){$('urls').placeholder='无需输入链接';message(mode==='xhs-homefeed'?'使用所选小红书账号读取首页摘要；完整正文请继续采集帖子详情。':mode==='bili-trending'?'读取当前热搜快照；热搜词不是视频或评论。':'读取首页推荐；有限样本不代表全部推荐内容。');return;}
  if(platformDiscovery){$('urls').placeholder=qa?(mode==='qa-search'?'搜索问题，例如 Python list comprehension':'问题数字 ID，每行一个'):['bili-hot','tieba-hot'].includes(mode)?'无需输入，读取公开热门列表':mode==='reddit-hot'?'社区名称，例如 AskScience':'Reddit 帖子链接';message(qa?'读取公开问题与社区回答；采纳状态和投票不代表事实正确率':mode==='tieba-hot'?'读取贴吧公开热议话题；话题不是帖子，也不是个性化动态':mode==='bili-hot'?'公开热门列表，不代表个性化首页':'按平台实际访问结果保存内容与覆盖限制');return;}
  if(discovery){message(mode==='seed'?'从 robots 与站点地图发现链接':'从站点地图与首页发现链接');return;}
  if(['model','research'].includes(mode)&&!answerAvailability[$('answer-backend').value]){message('所选回答方式暂不可用，已取得的来源仍会保留。');return;}
  message(mode==='ocr'?'在本机识别图片与 PDF 中的文字':mode==='transcribe'?'在本机转写音频与视频，保留时间码':mode==='research'?'搜索、读取来源，再按所选方式回答':mode==='web-search'?'查询公共搜索引擎，返回来源链接':mode==='media'?'保存图片、视频或音频的直接链接':mode==='model'?'根据已采集资料回答，并保留来源':local?'检索已采集原文，未进行联网搜索':platform?'已迁移源码；实际可用范围以平台返回为准':'可开始采集');
}
async function loadStatus(){
  try{
    const data=await api('status');handlers=data.handlers;platformCaps=data.modules.platforms?.capabilities||{};
    $('version').textContent='v'+data.version;
    for(const [code,cap] of Object.entries(platformCaps)){
      if(![...$('mode').options].some(o=>o.value===code))$('mode').add(new Option(cap.name,code));
      const option=[...$('mode').options].find(o=>o.value===code);option.textContent=cap.name;option.disabled=!cap.implemented;
    }
    for(const [mode,entry] of Object.entries(homepageModes)){
      if(![...$('mode').options].some(o=>o.value===mode))$('mode').add(new Option(entry.label,mode));
      document.querySelector(`#mode option[value="${mode}"]`).disabled=!data.modules.platform_discovery?.capabilities?.platforms?.[entry.platform]?.modes?.includes(entry.mode);
    }
    const modelConfigured=data.model?.available||data.model?.configured;
    answerAvailability={local:!!modelConfigured,codex:!!data.codex_model?.available};
    document.querySelector('#mode option[value="model"]').textContent='已采集资料问答';
    document.querySelector('#mode option[value="model"]').disabled=!handlers.includes('answer.model');
    document.querySelector('#mode option[value="research"]').disabled=!handlers.includes('research.answer');
    document.querySelector('#mode option[value="web-search"]').disabled=!handlers.includes('web.search');
    document.querySelector('#mode option[value="media"]').disabled=!handlers.includes('media.download');
    for(const [mode,label] of [['seed','发现站点网址'],['map','探索网站结构']]){
      if(![...$('mode').options].some(o=>o.value===mode))$('mode').add(new Option(label,mode));
      document.querySelector(`#mode option[value="${mode}"]`).disabled=!handlers.includes('web.'+mode);
    }
    for(const [mode,label,handler] of [['web-extract','网页结构化提取','web.collect'],['bili-hot','Bilibili 热门内容','platform.discover'],['tieba-hot','贴吧热议话题','platform.discover'],['reddit-hot','Reddit 社区热门','platform.discover'],['reddit-qa','Reddit 问答讨论','platform.discover'],['qa-search','Stack Exchange 问题搜索','platform.discover'],['qa-detail','Stack Exchange 问题与回答','platform.discover']]){
      if(![...$('mode').options].some(o=>o.value===mode))$('mode').add(new Option(label,mode));
      document.querySelector(`#mode option[value="${mode}"]`).disabled=!handlers.includes(handler)||(mode==='web-extract'&&!modelConfigured);
    }
    for(const [mode,label,handler] of [['vision','理解图片与视频','media.vision'],['image-generate','生成图片','creation.generate'],['image-edit','编辑图片','creation.edit']]){
      if(![...$('mode').options].some(o=>o.value===mode))$('mode').add(new Option(label,mode));
      document.querySelector(`#mode option[value="${mode}"]`).disabled=!handlers.includes(handler);
    }
    for(const [mode,label,handler] of [['semantic','本地语义检索','search.semantic'],['web-semantic','网页语义分组','web.collect']]){
      if(![...$('mode').options].some(o=>o.value===mode))$('mode').add(new Option(label,mode));
      document.querySelector(`#mode option[value="${mode}"]`).disabled=!handlers.includes(handler);
    }
    for(const [mode,handler] of [['ocr','media.ocr'],['transcribe','media.transcribe']]){
      if(![...$('mode').options].some(o=>o.value===mode))$('mode').add(new Option(mode==='ocr'?'识别图片与 PDF 文字':'转写音频与视频',mode));
      document.querySelector(`#mode option[value="${mode}"]`).disabled=!handlers.includes(handler);
    }
    const names={web:'通用网页',platforms:'平台采集',search:'本地检索',analysis:'本地模型',accounts:'账号与会话',discovery:'联网搜索',media:'媒体文件',media_analysis:'图片与语音识别',comment_reports:'评论分析与报告'};
    $('capabilities').replaceChildren();
    for(const [key,label] of Object.entries(names)){
      const ready=data.modules[key]?.status==='registered';let status=ready?'已接入':'开发中';
      if(key==='platforms')status=Object.values(platformCaps).filter(c=>c.implemented).length+' 个平台源码';
      if(key==='analysis')status=data.model?.available?'本地模型已就绪':modelConfigured?'本地按需加载':'本地模型未就绪';
      const li=node('li');li.append(node('span',label),node('em',status,ready?'ready':''));$('capabilities').append(li);
    }
    const codexRow=node('li');codexRow.append(node('span','Codex 账号'),node('em',data.codex_model?.available?'已登录 · 使用现有额度':'暂不可用',data.codex_model?.available?'ready':''));$('capabilities').append(codexRow);
    $('submit').disabled=!handlers.includes('web.collect');await loadAccounts();await loadExportTargets();updateMode();
  }catch(error){message('连接失败：'+error.message,true);}
}
async function loadAccounts(){
  accounts=(await api('accounts')).accounts;$('account-list').replaceChildren();
  const labels={not_logged_in:'尚未登录',saved_unverified:'已保存，待平台验证',revoked:'已撤销',verified:'最近校验通过',auth_required:'需要重新验证',validation_unknown:'暂时无法确认'};
  for(const account of accounts){
    const row=node('div',undefined,'account-row');row.append(node('span',(platformCaps[account.platform]?.name||account.platform)+' · '+account.account),node('span',labels[account.status]||account.status));
    if(handlers.includes('account.check')&&!['revoked','not_logged_in'].includes(account.status)){
      const check=node('button','校验会话');check.type='button';check.addEventListener('click',async()=>{check.disabled=true;try{const job=await api('jobs',{method:'POST',body:JSON.stringify({kind:'account.check',params:{platform:account.platform,account:account.account,max_seconds:60}})});selected=job.id;await refresh();}catch(error){message(error.message,true);}finally{check.disabled=false;}});row.append(check);
    }
    if(['auth_required','revoked','validation_unknown'].includes(account.status)&&handlers.includes('account.login')){
      const login=node('button','重新登录');login.type='button';login.addEventListener('click',async()=>{try{const job=await api('jobs',{method:'POST',body:JSON.stringify({kind:'account.login',params:{platform:account.platform,account:account.account}})});selected=job.id;await refresh();}catch(error){message(error.message,true);}});row.append(login);
    }
    if(account.status!=='revoked'){const revoke=node('button','撤销本机缓存');revoke.type='button';revoke.addEventListener('click',async()=>{try{await api(`accounts/${account.platform}/${account.account}`,{method:'DELETE'});await loadAccounts();}catch(error){message(error.message,true);}});row.append(revoke);}
    $('account-list').append(row);
  }
  updateAccounts();
}
async function loadJobs(){
  const data=await api('jobs');$('jobs').replaceChildren();
  if(!data.jobs.length){$('jobs').append(node('p','还没有采集任务。','empty'));return;}
  for(const job of data.jobs){
    const value=job.params.urls?.[0]||job.params.url||job.params.files?.[0]||job.params.ids?.[0]||job.params.creators?.[0]||job.params.keywords?.[0]||job.params.question||job.params.query||job.kind;
    const label=['account.login','account.check'].includes(job.kind)?(job.kind==='account.check'?'校验会话 · ':'登录 · ')+(platformCaps[job.params.platform]?.name||job.params.platform)+' / '+(job.params.account||'main'):typeof value==='object'?(value.url||value.path||job.kind):value;
    const row=node('div',undefined,'job-row'),button=node('button',label);button.type='button';button.dataset.jobId=job.id;button.addEventListener('click',()=>showJob(job.id));
    row.append(button,node('time',new Date(job.created_at).toLocaleString('zh-CN',{hour12:false})),node('span',states[job.status]||job.status,'badge '+job.status));$('jobs').append(row);
  }
}
function sourceLink(url,title){if(!/^https?:\/\//i.test(url||''))return null;const a=node('a',title||url);a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;}
async function saveArtifact(record,index,name,size){
  try{
    const jobId=selected;
    const handle=size>16*1024*1024&&window.showSaveFilePicker?await window.showSaveFilePicker({suggestedName:name}):null;
    const response=await fetch('/api/jobs/'+jobId+'/files/'+record.record_key+'/'+index,{headers:{'X-Research-Token':token}});
    if(!response.ok)throw new Error('文件暂不可用');
    if(handle){await response.body.pipeTo(await handle.createWritable());}
    else{const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  }catch(error){if(error.name!=='AbortError')message(error.message,true);}
}
function orderTopicRecords(records){
  const isTopic=record=>['tieba','bili'].includes(record.source)&&record.kind==='topic';
  const rank=record=>Number.isInteger(record.coverage?.rank_at_observation)&&record.coverage.rank_at_observation>0?record.coverage.rank_at_observation:Infinity;
  const groups={};for(const record of records.filter(isTopic))(groups[record.source]??=[]).push(record);
  for(const group of Object.values(groups))group.sort((a,b)=>rank(a)-rank(b));
  return records.map(record=>isTopic(record)?groups[record.source].shift():record);
}
function renderRecord(record){
  const recordJob=selected;
  const p=record.payload, article=node('article',undefined,'record');
  const title=p.title||(record.source==='stackexchange'?({question:'社区问题',answer:'社区回答',comment:'社区评论'}[record.kind]):null)||({comment:'评论',evidence_answer:'来源证据摘录',answer:'模型回答',research_answer:'模型回答',search_result:'本地检索结果',semantic_result:'语义检索结果',media_asset:'媒体文件',ocr:'识别文字',transcript:'语音转写',report:'分析报告',comment_annotation:'评论标签',visual_claim:'画面分析',generate:'生成图片',edit:'参考图再创作'}[record.kind]||record.native_id);
  article.append(node('h3',title));const link=sourceLink(record.url,record.source==='tieba'&&record.kind==='topic'?'查看话题':undefined);if(link)article.append(link);
  if(record.source==='tieba'&&record.kind==='topic'){
    const raw=p.raw_topic||{},rank=record.coverage?.rank_at_observation,metrics=[];
    article.append(node('p','贴吧热议话题','metrics'));
    if(raw.topic_desc||raw.abstract)article.append(node('pre',raw.topic_desc||raw.abstract));
    if(rank!=null)metrics.push(`列表位置 ${rank}`);
    for(const key of ['idx_num','discuss_num'])if(raw[key]!=null)metrics.push(`${key} ${raw[key]}`);
    if(metrics.length)article.append(node('p',metrics.join(' · '),'metrics'));
    article.append(node('small','列表位置按本次榜单顺序；idx_num、discuss_num 保留平台原值，计数口径未核实，不作为评论数。'));
  }
  if(record.source==='bili'&&record.kind==='topic'){
    const rank=record.coverage?.rank_at_observation,heat=p.raw_topic?.heat_score,metrics=[];
    if(rank!=null)metrics.push(`热搜位置 ${rank}`);
    if(heat!=null)metrics.push(`heat_score ${heat}`);
    if(metrics.length)article.append(node('p',metrics.join(' · '),'metrics'));
    article.append(node('small','热度保留平台原值，不代表播放量或评论数。'));
  }
  if(p.source_usable===false)article.append(node('p',p.score_selection?.selected===false?'发现分数低于阈值 · 已排除当前结果':p.blocking?.blocked?'疑似拦截页 · 已排除检索与回答':'HTTP 请求未成功 · 已排除检索与回答','metrics'));
  const text=p.markdown||p.text||p.content||p.desc||(['answer','research_answer'].includes(record.kind)?p.answer:p.evidence?p.label:p.answer)||p.message||p.label||'';
  if(text)article.append(node('pre',text));if(p.limits)article.append(node('p',p.limits,'metrics'));
  if(['answer','research_answer'].includes(record.kind)&&!p.answer&&!p.limits)article.append(node('p','尚未生成有来源支持的回答。','metrics'));
  if(p.claims?.some(claim=>claim.supports?.length)){
    const section=node('details');section.append(node('summary','逐条核对引用'));
    for(const claim of p.claims){
      section.append(node('p',claim.text));
      for(const support of claim.supports||[]){
        section.append(node('blockquote',support.quote));
        const source=(p.evidence||[]).find(item=>item.citation===support.citation);
        if(source){const link=sourceLink(source.url,`[${support.citation}] ${source.title||'查看原文'}`);if(link)section.append(link);}
      }
    }
    article.append(section);
  }
  if(p.llm_extraction){const section=node('details');section.append(node('summary','模型结构化提取 · '+(p.llm_extraction.status==='complete'?'处理完成':'部分完成')),node('pre',JSON.stringify(p.llm_extraction.records?.map(r=>r.data)||[],null,2)),node('small','模型提取结果；请核对上方原文。'));article.append(section);}
  if(p.crawler){const section=node('details');section.append(node('summary','专用提取 · '+p.crawler.name),node('pre',JSON.stringify({records:p.crawler.records,limits:p.crawler.limits},null,2)));article.append(section);}
  if(p.local_content_filter){const result=p.local_content_filter,derived=result.mode==='local_html_markdown_transform',section=node('details');section.append(node('summary',(derived?'整理后的 Markdown':'相关原文')+' · '+(result.status==='complete'?'处理完成':'部分完成')),node('pre',(derived?result.derived_markdown:result.fit_markdown)||''),node('small',derived?'模型整理内容；引用指向原始 HTML，不代表语义已核验。上方原文保持不变。':'模型选择的原文片段；完整原文保留在上方。'));article.append(section);}
  if(p.adaptive){const section=node('details');section.append(node('summary','探索依据 · '+p.adaptive.documents+' 篇来源'),node('p',p.adaptive.query),node('pre',JSON.stringify(p.adaptive.metrics,null,2)),node('small','统计分数用于决定后续探索，不代表事实正确率或全站覆盖率。'));article.append(section);}
  if(p.schema_inference){const section=node('details');section.append(node('summary','自动提取规则 · '+(p.schema_inference.status==='complete'?'生成完成':'部分完成')),node('pre',JSON.stringify({schema:p.schema_inference.schema,records:p.schema_inference.records},null,2)));article.append(section);}
  if(p.llm_tables){const section=node('details');section.append(node('summary','表格提取 · '+(p.llm_tables.status==='complete'?'处理完成':'部分完成')));for(const table of p.llm_tables.tables||[]){section.append(node('pre',JSON.stringify({headers:table.headers,rows:table.rows},null,2)));if(String(table.extraction_method).includes('source_grid'))section.append(node('small','包含按原始单元格结构还原的内容；对应模型候选未通过校验。'));if(table.header_rows||table.nested_tables?.length){const structure=node('details');structure.append(node('summary','表头与嵌套关系'),node('pre',JSON.stringify({table_index:table.table_index,header_rows:table.header_rows,header_paths:table.header_paths,nested_tables:table.nested_tables},null,2)));section.append(structure);}}const rejected=(p.llm_tables.attempts||[]).filter(attempt=>attempt.status!=='complete');if(rejected.length){const diagnostic=node('details');diagnostic.append(node('summary','未采用的模型候选'),node('pre',JSON.stringify(rejected,null,2)));section.append(diagnostic);}section.append(node('small','值已回查来源单元格；结构关系与完整性仍需核对。'));article.append(section);}
  if(p.semantic_extraction){const section=node('details');section.append(node('summary','语义分组 · '+(p.semantic_extraction.status==='complete'?'处理完成':'部分完成')));for(const group of p.semantic_extraction.clusters||[]){section.append(node('pre',group.content));if(group.tags?.length)section.append(node('small',group.tags.join(' · ')));}section.append(node('small','分组与标签是模型判断；不代表已验证主题。'));article.append(section);}
  if(p.c4a_result){article.append(node('p','页面脚本：'+(p.c4a_result.success?'执行完成':p.c4a_result.status),'metrics'));if(p.c4a_result.errors?.length)article.append(node('pre',JSON.stringify(p.c4a_result.errors,null,2)));}
  if(p.model_claims)article.append(node('small','模型视觉描述，尚未核验；视频结果仅覆盖采样帧。'));
  if(p.frame_evidence?.timecode_seconds!=null)article.append(node('p',`视频 ${Number(p.frame_evidence.timecode_seconds).toFixed(2)} 秒`,'metrics'));
  if(p.link_preview){const section=node('details');section.append(node('summary','链接预览'),node('pre',JSON.stringify(p.link_preview.results||[],null,2)));article.append(section);}
  const facts={liked_count:'赞',video_comment:'评论',video_share_count:'分享',video_coin_count:'投币',video_favorite_count:'收藏',video_play_count:'播放'};
  const metrics=Object.entries(facts).filter(([key])=>p[key]!=null).map(([key,label])=>`${label} ${p[key]}`);if(metrics.length)article.append(node('p',metrics.join(' · '),'metrics'));
  const evidence=p.results||p.evidence||[];
  for(const item of Array.isArray(evidence)?evidence:[evidence]){const source=node('div',undefined,'evidence-source'),a=sourceLink(item.url,(item.citation?'['+item.citation+'] ':'')+(item.title||item.url));if(a)source.append(a);source.append(node('pre',item.excerpt||item.text||''));article.append(source);}
  if(p.labels){
    const words={positive:'正面',negative:'负面',neutral:'中性',mixed:'混合',unknown:'未知',praise:'赞扬',complaint:'抱怨',question:'提问',request:'请求',share:'分享',humor:'幽默',other:'其他',support:'支持',oppose:'反对',content:'内容',quality:'质量',usability:'易用性',price:'价格',service:'服务',creator:'创作者'};
    article.append(node('p',['情绪','意图','立场'].map((name,index)=>name+'：'+(words[p.labels[['emotion','intent','stance'][index]]?.label]||'未知')).join(' · '),'metrics'));
    article.append(node('small','模型标签属于分析判断；不代表全部评论或用户意见。'));
  }
  if(p.analysis?.segments){const transcript=p.analysis.segments.map(segment=>`${segment.start.toFixed(1)}–${segment.end.toFixed(1)} 秒  ${segment.text}`).join('\n');article.append(node('pre',transcript));}
  const files=p.files||Object.entries(p.artifacts||{}).sort(([a],[b])=>a.localeCompare(b)).map(([kind,path])=>({path,filename:path.split(/[\\/]/).pop(),role:kind}));files.forEach((file,index)=>{const filename=file.filename||file.path?.split(/[\\/]/).pop()||'artifact';article.append(node('small',file.path||filename));const button=node('button','保存 '+(file.role||filename));button.addEventListener('click',()=>saveArtifact(record,index,filename,file.bytes||file.size||0));article.append(button);});
  files.forEach((file,index)=>{if(!/\.(png|jpe?g|webp)$/i.test(file.path||file.filename||''))return;const button=node('button','查看图片');button.type='button';button.addEventListener('click',async()=>{button.disabled=true;try{const response=await fetch(`/api/jobs/${recordJob}/files/${record.record_key}/${index}`,{headers:{'X-Research-Token':token}});if(!response.ok)throw new Error('图片暂不可用');const url=URL.createObjectURL(await response.blob()),img=node('img');img.alt='任务图片';img.className='artifact-image';img.onload=img.onerror=()=>URL.revokeObjectURL(url);img.src=url;button.replaceWith(img);}catch(error){button.disabled=false;message(error.message,true);}});article.append(button);});
  files.forEach(file=>{if(file.role!=='generated_image'||!file.path||!/^[a-f0-9]{64}$/i.test(file.sha256||''))return;const button=node('button','继续编辑');button.type='button';button.addEventListener('click',()=>prepareGeneratedImageEdit(file));article.append(button);});
  article.append(node('small',`采集于 ${record.observed_at} · ${record.source} / ${record.kind}`));return article;
}
async function showJob(id){
  selected=id;
  try{
    const data=await api('jobs/'+id);
    const signature=JSON.stringify([id,data.job.updated_at,data.counts,data.waiting_for_login,exportTargets.map(t=>t.revision)]);
    if(signature===renderedJobSignature)return;
    renderedJobSignature=signature;
    $('detail').hidden=false;$('detail-title').textContent=states[data.job.status]||data.job.status;
    const summary=data.job.summary||{};
    $('detail-meta').textContent=`${data.counts.records} 条记录 · ${data.counts.versions} 个版本`+(data.job.error?' · '+data.job.error.code:'')+(summary.reason?' · '+summary.reason:'');
    if(data.waiting_for_login)$('detail-meta').textContent='请在打开的浏览器中完成登录，再保存所选平台会话。';
    $('actions').replaceChildren();
    if(handlers.includes('export.remote')&&exportTargets.length&&data.records.length&&!['pending','running'].includes(data.job.status)){
      const target=node('select');target.setAttribute('aria-label','导出数据库');
      for(const item of exportTargets)target.add(new Option(item.name+' · '+item.backend,item.name));
      const button=node('button','导出到数据库');button.addEventListener('click',async()=>{try{const job=await api('jobs',{method:'POST',body:JSON.stringify({kind:'export.remote',params:{source_job_id:id,target_name:target.value}})});selected=job.id;await refresh();}catch(error){message(error.message,true);}});
      $('actions').append(target,button);
    }
    const allowed=data.job.status==='running'?['pause','cancel']:['pending','partial','paused','interrupted','failed','auth_required'].includes(data.job.status)?['resume','cancel']:[];
    const itemLimit=data.resume_limits?.max_items;
    if(data.waiting_for_login)allowed.unshift('confirm-login');
    for(const action of allowed){const b=node('button',{pause:'暂停',cancel:'取消',resume:itemLimit?.can_increase?'按原上限继续':'继续','confirm-login':'我已完成登录'}[action]);b.addEventListener('click',async()=>{try{await api(`jobs/${id}/${action}`,{method:'POST'});await refresh();await loadAccounts();}catch(error){message(error.message,true);}});$('actions').append(b);}
    if(itemLimit?.can_increase){
      const group=node('div',undefined,'resume-limit'),label=node('label',`新上限（原 ${itemLimit.current}）`),input=node('input');
      input.type='number';input.required=true;input.min=String(itemLimit.minimum);input.max=String(itemLimit.maximum);input.step='1';
      input.value=String(Math.min(itemLimit.maximum,Math.max(itemLimit.minimum,itemLimit.current*2)));
      input.id='resume-max-items';input.setAttribute('aria-label','新的采集条数上限');label.append(input);
      const extend=node('button','提高上限并继续');extend.type='button';extend.id='resume-with-limit';
      extend.addEventListener('click',async()=>{
        if(!input.reportValidity()||!Number.isInteger(input.valueAsNumber))return;
        const nextLimit=input.valueAsNumber;extend.disabled=true;input.disabled=true;
        try{await api(`jobs/${id}/resume`,{method:'POST',body:JSON.stringify({max_items:nextLimit})});renderedJobSignature=null;await refresh();message(`已将采集条数上限提高到 ${nextLimit}，继续原任务。`);}
        catch(error){extend.disabled=false;input.disabled=false;message(error.message,true);}
      });
      group.append(label,extend);$('actions').append(group);
    }
    if(handlers.includes('media.download')&&data.records.some(r=>r.payload.media?.length)){const media=node('button','保存帖子媒体');media.addEventListener('click',async()=>{try{const params={source_job_id:id,max_files:20,max_bytes:100000000};if(data.job.params.account)params.account=data.job.params.account;const job=await api('jobs',{method:'POST',body:JSON.stringify({kind:'media.download',params})});selected=job.id;await refresh();}catch(error){message(error.message,true);}});$('actions').append(media);}
    const nextJobs=[];
    if(data.records.some(r=>r.kind==='comment'))nextJobs.push(['comments.analyze','分析评论'],
      ['comments.analyze','Codex 分析评论',{mode:'local_model',answer_backend:'codex',batch_size:3,max_seconds:600}],['comments.wordcloud','生成词云']);
    if(data.records.length&&data.job.kind!=='export.records')nextJobs.push(['export.records','导出更多格式']);
    if(data.records.length&&data.records.every(r=>r.kind==='page'&&['web','legacy_cache'].includes(r.source)))nextJobs.push(['cache.export','导出 Crawl4AI 缓存']);
    if(data.job.kind==='comments.analyze'&&data.records.length)nextJobs.push(['report.build','生成报告']);
    const mediaFiles=data.records.filter(r=>r.kind==='media_asset').flatMap(r=>r.payload.files||[]);
    if(mediaFiles.some(f=>/\.(png|jpe?g|webp|bmp|tiff?|pdf)$/i.test(f.path||'')))nextJobs.push(['media.ocr','识别文件文字']);
    if(mediaFiles.some(f=>/\.(wav|mp3|m4a|aac|flac|ogg|opus|mp4|mkv|webm|mov)$/i.test(f.path||'')))nextJobs.push(['media.transcribe','转写媒体语音']);
    if(mediaFiles.some(f=>/\.(png|jpe?g|webp|mp4|mkv|webm|mov)$/i.test(f.path||'')))nextJobs.push(['media.vision','理解媒体内容']);
    for(const [kind,label,options] of nextJobs.filter(([kind])=>handlers.includes(kind))){const button=node('button',label);if(options?.answer_backend==='codex')button.title='使用已登录 Codex 账户的额度，分组分析评论';button.addEventListener('click',async()=>{try{const params={source_job_id:id,...options};if(kind==='export.records')params.formats=['json','jsonl','csv','excel','sqlite','sqlite_native'];const job=await api('jobs',{method:'POST',body:JSON.stringify({kind,params})});selected=job.id;await refresh();}catch(error){message(error.message,true);}});$('actions').append(button);}
    const download=node('button','导出 JSON');download.addEventListener('click',async()=>{try{const result=await api(`jobs/${id}/export/json`),url=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download=`research-${id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(error){message(error.message,true);}});$('actions').append(download);
    $('records').replaceChildren(...orderTopicRecords(data.records).map(renderRecord));
    if(!data.records.length)$('records').append(node('p',data.waiting_for_login?'登录凭据不会出现在采集结果中。':data.job.status==='running'?'正在处理，结果会逐页保存。':summary.message||(['account.check','account.login'].includes(data.job.kind)&&data.job.status==='auth_required'?'请在账号与登录中重新登录所选平台。':summary.account?'会话已保存，平台有效性仍以校验结果为准。':'暂无已保存的内容。'),'empty'));
    if(['account.check','account.login'].includes(data.job.kind)&&!['pending','running'].includes(data.job.status))await loadAccounts();
  }catch(error){message(error.message,true);}
}
async function refresh(){if(polling)return;polling=true;try{await loadJobs();if(selected)await showJob(selected);}catch(error){message(error.message,true);}finally{polling=false;}}
async function loadExportTargets(){
  if(!handlers.includes('export.remote'))return;
  exportTargets=(await api('export-targets')).targets;
  $('database-list').replaceChildren();
  for(const target of exportTargets){
    const row=node('div',undefined,'account-row'),remove=node('button','移除配置');remove.type='button';
    row.append(node('span',target.name+' · '+target.backend),remove);
    remove.addEventListener('click',async()=>{try{await api('export-targets/'+encodeURIComponent(target.name),{method:'DELETE'});await loadExportTargets();}catch(error){$('database-message').textContent=error.message;}});
    $('database-list').append(row);
  }
}
{
  const label=node('label','提取目标');label.id='extraction-options';label.hidden=true;
  const input=node('textarea');input.id='extraction-instruction';input.rows=2;input.placeholder='例如：提取名称、日期与关键数据';label.append(input);
  $('urls').after(label);
  const webOptions=node('details');webOptions.id='web-options';webOptions.append(node('summary','网页设置'));
  const webFields=node('div',undefined,'field-row');webOptions.append(webFields);
  const engineLabel=node('label','浏览器'),engine=node('select');engine.id='web-engine';for(const [value,title] of [['','自动 / Chromium'],['firefox','Firefox'],['webkit','WebKit'],['patchright','Patchright Chromium']])engine.add(new Option(title,value));engineLabel.append(engine);webFields.append(engineLabel);
  for(const [id,title] of [['web-downloads','接收页面下载'],['web-diagnostics','保存浏览器诊断']]){const option=node('label'),input=node('input');input.type='checkbox';input.id=id;option.append(input,document.createTextNode(title));webFields.append(option);}
  const chunkLabel=node('label','文本分段'),chunkSelect=node('select');chunkSelect.id='web-chunking';for(const [value,title] of [['','默认'],['nlp_sentence','句子分段（NLTK）'],['nlp_topic','主题段落（TextTiling）']])chunkSelect.add(new Option(title,value));chunkLabel.append(chunkSelect);webFields.append(chunkLabel);
  const profileLabel=node('label','浏览器配置'),profile=node('input');profile.id='web-profile';profile.placeholder='可选，例如 research';profile.pattern='[A-Za-z0-9][A-Za-z0-9_.-]{0,99}';profileLabel.append(profile);webFields.append(profileLabel);
  const identityOptions=node('details');identityOptions.append(node('summary','代理与 User-Agent'));webOptions.append(identityOptions);
  const identityFields=node('div',undefined,'field-row');identityOptions.append(identityFields);
  const proxyLabel=node('label','已登记的代理别名'),proxyNames=node('input');proxyNames.id='web-proxy-names';proxyNames.placeholder='可选，多个别名用逗号分隔';proxyLabel.append(proxyNames);identityFields.append(proxyLabel);
  const proxyStrategyLabel=node('label','代理分配'),proxyStrategy=node('select');proxyStrategy.id='web-proxy-strategy';proxyStrategy.add(new Option('轮流分配','round_robin'));proxyStrategy.add(new Option('同一站点保持','sticky'));proxyStrategyLabel.append(proxyStrategy);identityFields.append(proxyStrategyLabel);
  const uaLabel=node('label','User-Agent'),uaMode=node('select');uaMode.id='web-ua-mode';for(const [value,title] of [['','默认'],['explicit','指定内容'],['random','本地浏览器资料库']])uaMode.add(new Option(title,value));uaLabel.append(uaMode);identityFields.append(uaLabel);
  const uaTextLabel=node('label','指定 User-Agent'),uaText=node('input');uaText.id='web-ua-value';uaTextLabel.hidden=true;uaTextLabel.append(uaText);identityFields.append(uaTextLabel);uaMode.addEventListener('change',()=>{uaTextLabel.hidden=uaMode.value!=='explicit';uaText.required=uaMode.value==='explicit';});
  const adaptiveLabel=node('label'),adaptive=node('input');adaptive.type='checkbox';adaptive.id='web-adaptive';adaptiveLabel.append(adaptive,document.createTextNode('按可用内存调度'));webFields.append(adaptiveLabel);
  const streamLabel=node('label'),stream=node('input');stream.type='checkbox';stream.id='web-stream';streamLabel.append(stream,document.createTextNode('先显示已完成页面'));webFields.append(streamLabel);
  const depthLabel=node('label','链接探索层数'),depth=node('input');depth.type='number';depth.id='web-depth';depth.min='0';depth.max='10';depth.value='0';depthLabel.append(depth);webFields.append(depthLabel);
  const topicLabel=node('label','自适应探索主题'),topic=node('input');topic.id='web-topic';topic.placeholder='可选：根据相关性与新增信息调整探索';topicLabel.append(topic);webOptions.append(topicLabel);
  const explorationLabel=node('label','探索方式'),exploration=node('select');exploration.id='web-exploration';exploration.add(new Option('词语统计','statistical'));exploration.add(new Option('本地语义','embedding'));explorationLabel.append(exploration);webFields.append(explorationLabel);
  const expansionLabel=node('label'),expansion=node('input');expansion.type='checkbox';expansion.id='web-query-expansion';expansion.disabled=true;expansionLabel.append(expansion,document.createTextNode('用本地模型扩展探索问题'));webFields.append(expansionLabel);exploration.addEventListener('change',()=>{expansion.disabled=exploration.value!=='embedding';});
  const crawlerLabel=node('label','专用提取'),crawler=node('select');crawler.id='web-crawler';for(const [value,title] of [['','通用网页'],['amazon_product','Amazon 商品'],['google_search','Google 搜索结果'],['google_images','Google 图片结果']])crawler.add(new Option(title,value));crawlerLabel.append(crawler);webFields.append(crawlerLabel);
  const filterModeLabel=node('label','内容处理'),filterMode=node('select');filterMode.id='web-filter-mode';filterMode.add(new Option('选取相关原文','extractive'));filterMode.add(new Option('整理 HTML 为 Markdown','transform'));filterModeLabel.append(filterMode);webFields.append(filterModeLabel);
  const filterLabel=node('label','内容处理要求'),filter=node('input');filter.id='web-filter';filter.placeholder='可选：例如保留研究结果与限制，按主题分组';filterLabel.append(filter);webOptions.append(filterLabel);
  const schemaLabel=node('label','自动生成提取规则'),schema=node('input');schema.id='web-schema';schema.placeholder='可选：例如提取文章名称、金额和链接网址';schemaLabel.append(schema);webOptions.append(schemaLabel);
  const tablesLabel=node('label'),tables=node('input');tables.id='web-tables';tables.type='checkbox';tablesLabel.append(tables,document.createTextNode('使用本地模型解析复杂表格'));webFields.append(tablesLabel);
  const stitchLabel=node('label'),stitch=node('input');stitch.id='web-stitch';stitch.type='checkbox';stitchLabel.append(stitch,document.createTextNode('保存拼接长截图'));webFields.append(stitchLabel);
  const blockLabel=node('label'),block=node('input');block.id='web-detect-blocking';block.type='checkbox';blockLabel.append(block,document.createTextNode('识别疑似拦截页并排除回答'));webFields.append(blockLabel);
  engine.addEventListener('change',()=>{const limited=['firefox','webkit'].includes(engine.value);$('web-downloads').disabled=limited;profile.disabled=limited;if(limited)message('该浏览器在响应完成后计费，未知长度响应可能超过字节预算。');});
  const profiles=node('details');profiles.append(node('summary','已保存的浏览器配置'));const profileList=node('div');profileList.id='browser-profile-list';profiles.append(profileList);webOptions.append(profiles);
  profiles.addEventListener('toggle',async()=>{if(!profiles.open)return;try{const data=await api('browser-profiles');profileList.replaceChildren();for(const item of data.profiles){const row=node('div',undefined,'field-row');row.append(node('span',item.name+(item.busy?' · 使用中':'')));const remove=node('button','删除配置');remove.type='button';remove.disabled=item.busy;remove.addEventListener('click',async()=>{try{await api('browser-profiles/'+encodeURIComponent(item.name),{method:'DELETE'});if(profile.value.trim()===item.name)profile.value='';row.remove();}catch(error){message(error.message,true);}});row.append(remove);profileList.append(row);}if(!data.profiles.length)profileList.append(node('p','尚无保存的配置。'));}catch(error){message(error.message,true);}});
  const c4aLabel=node('label','页面操作脚本（C4A）'),c4a=node('textarea');c4a.id='web-c4a';c4a.rows=3;c4a.placeholder='WAIT `#content` 5';c4aLabel.append(c4a);webOptions.append(c4aLabel);label.after(webOptions);
  const discoveryOptions=node('details');discoveryOptions.id='discovery-options';discoveryOptions.hidden=true;discoveryOptions.append(node('summary','网址发现来源'));
  const qaOptions=node('div',undefined,'field-row');qaOptions.id='qa-options';qaOptions.hidden=true;const siteLabel=node('label','问答站点'),site=node('input');site.id='qa-site';site.value='stackoverflow';site.pattern='[a-z0-9][a-z0-9.\\-]{0,99}';siteLabel.append(site);qaOptions.append(siteLabel);const commentsLabel=node('label'),qaComments=node('input');qaComments.id='qa-comments';qaComments.type='checkbox';commentsLabel.append(qaComments,document.createTextNode('同时读取公开评论'));qaOptions.append(commentsLabel);webOptions.after(qaOptions);
  for(const [id,title] of [['cc','Common Crawl'],['wayback','互联网档案馆'],['crt','证书透明度记录']]){const row=node('label'),input=node('input');input.id='discovery-'+id;input.type='checkbox';row.append(input,document.createTextNode(title));discoveryOptions.append(row);}webOptions.after(discoveryOptions);
  const retrievalLabel=node('label','资料检索方式');retrievalLabel.id='retrieval-option';retrievalLabel.hidden=true;const retrieval=node('select');retrieval.id='model-retrieval';retrieval.add(new Option('文字匹配','lexical'));retrieval.add(new Option('语义匹配','semantic'));retrievalLabel.append(retrieval);webOptions.after(retrievalLabel);
  const answerLabel=node('label','回答方式');answerLabel.id='answer-backend-option';answerLabel.hidden=true;const answerBackend=node('select');answerBackend.id='answer-backend';answerBackend.add(new Option('Codex 账号（联网、使用现有额度）','codex'));answerBackend.add(new Option('本地模型（实验）','local'));answerBackend.value='codex';answerBackend.addEventListener('change',updateMode);answerLabel.append(answerBackend);webOptions.after(answerLabel);
  const imageOptions=node('div',undefined,'field-row');imageOptions.id='image-options';imageOptions.hidden=true;
  const referenceLabel=node('label','参考图片路径'),reference=node('input');reference.id='image-reference';reference.placeholder='F:\\…';referenceLabel.append(reference);imageOptions.append(referenceLabel);
  reference.addEventListener('input',clearImageReferenceHash);
  const methodLabel=node('label','编辑方式'),editMethod=node('select');editMethod.id='image-edit-method';editMethod.add(new Option('指令编辑','instruction'));editMethod.add(new Option('参考图再创作','img2img'));editMethod.value='instruction';editMethod.addEventListener('change',updateMode);methodLabel.append(editMethod);imageOptions.prepend(methodLabel);
  const seedLabel=node('label','随机种子'),seed=node('input');seed.id='image-seed';seed.type='number';seed.min='0';seed.max='2147483644';seed.value='42';seedLabel.append(seed);imageOptions.append(seedLabel);label.after(imageOptions);
  const templateLabel=node('label','风格'),template=node('select');template.id='image-template';for(const [value,title] of [['','按画面描述'],['studio_object','静物摄影'],['botanical_watercolor','植物水彩'],['editorial_landscape','自然风景']])template.add(new Option(title,value));templateLabel.append(template);imageOptions.append(templateLabel);
  const section=node('section',undefined,'account-section'),details=node('details'),form=node('form',undefined,'field-row');
  const cacheSection=node('section',undefined,'account-section'),cacheDetails=node('details'),cacheForm=node('form',undefined,'field-row');cacheDetails.append(node('summary','导入已有 Crawl4AI 缓存'));cacheSection.append(cacheDetails);cacheDetails.append(cacheForm);$('history').before(cacheSection);
  for(const [id,title,required] of [['cache-database','SQLite 文件路径',true],['cache-content-root','内容目录（文件式缓存）',false],['cache-identity','来源身份标签',true]]){const label=node('label',title),input=node('input');input.id=id;input.required=required;input.placeholder=id==='cache-identity'?'例如 public-research':'F:\\…';label.append(input);cacheForm.append(label);}
  const formatLabel=node('label','缓存格式'),format=node('select');format.id='cache-format';format.add(new Option('独立内容文件','files'));format.add(new Option('旧版内联字段','inline'));formatLabel.append(format);cacheForm.append(formatLabel);const cacheSubmit=node('button','导入为历史资料');cacheSubmit.type='submit';cacheForm.append(cacheSubmit);const cacheStatus=node('p','按指定文件导入，缓存时间不会当作原始采集时间。','account-help');cacheStatus.id='cache-status';cacheStatus.setAttribute('role','status');cacheDetails.append(cacheStatus);
  cacheForm.addEventListener('submit',async event=>{event.preventDefault();cacheSubmit.disabled=true;try{const params={database:$('cache-database').value.trim(),identity_scope:$('cache-identity').value.trim(),format:$('cache-format').value,max_records:100};if($('cache-content-root').value.trim())params.content_root=$('cache-content-root').value.trim();const job=await api('jobs',{method:'POST',body:JSON.stringify({kind:'cache.import',params})});selected=job.id;cacheStatus.textContent='正在读取所选缓存';await refresh();}catch(error){cacheStatus.textContent=error.message;}finally{cacheSubmit.disabled=false;}});
  details.append(node('summary','数据库导出'));
  function field(title,id,type='text'){
    const label=node('label',title),input=node('input');input.id=id;input.type=type;input.autocomplete='off';label.append(input);form.append(label);return input;
  }
  field('配置名称','database-name').required=true;
  const backendLabel=node('label','类型'),backend=node('select');backend.id='database-backend';
  for(const [value,label] of [['mysql','MySQL'],['postgresql','PostgreSQL'],['mongodb','MongoDB']])backend.add(new Option(label,value));
  backendLabel.append(backend);form.append(backendLabel);
  field('连接地址','database-dsn','password').required=true;
  const database=field('MongoDB 数据库','database-database');database.parentElement.hidden=true;
  backend.addEventListener('change',()=>{database.parentElement.hidden=backend.value!=='mongodb';database.required=backend.value==='mongodb';});
  const submit=node('button','保存配置');submit.type='submit';form.append(submit);
  const status=node('p','凭据只保存到本机受保护配置中。保存配置不会连接数据库。','account-help');status.id='database-message';status.setAttribute('role','status');
  const list=node('div');list.id='database-list';details.append(form,status,list);section.append(details);$('history').before(section);
  form.addEventListener('submit',async event=>{
    event.preventDefault();submit.disabled=true;
    try{await api('export-targets',{method:'POST',body:JSON.stringify({name:$('database-name').value.trim(),backend:backend.value,dsn:$('database-dsn').value,database:database.value.trim()||null})});
      $('database-dsn').value='';status.textContent='配置已保存。选择采集任务后可导出到数据库。';await loadExportTargets();
    }catch(error){status.textContent=error.message;}finally{submit.disabled=false;}
  });
}
$('mode').addEventListener('change',updateMode);$('platform-mode').addEventListener('change',updateMode);
$('collect-form').addEventListener('submit',async event=>{
  event.preventDefault();const values=$('urls').value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);if(!values.length&&!Object.hasOwn(homepageModes,$('mode').value)&&!['bili-hot','tieba-hot'].includes($('mode').value))return;
  const mode=$('mode').value;let body;
  if(isPlatform()){
    const operation=$('platform-mode').value,params={platform:mode,mode:operation,max_items:Number($('max-posts').value),max_pages:Number($('max-pages').value),max_comments:Number($('max-comments').value),include_replies:$('include-replies').checked,max_replies:Number($('max-comments').value)};
    params[operation==='search'?'keywords':operation==='creator'?'creators':'ids']=values;
    body={kind:'platform.collect',params};
  }else if(Object.hasOwn(homepageModes,mode)){
    const entry=homepageModes[mode];body={kind:'platform.discover',params:{platform:entry.platform,mode:entry.mode,max_items:20,max_pages:Number($('max-pages').value),max_seconds:120,max_requests:12}};
  }else if(['qa-search','qa-detail'].includes(mode)){
    const params={platform:'stackexchange',site:$('qa-site').value.trim(),mode:mode==='qa-search'?'search':'detail',max_pages:Number($('max-pages').value),max_items:40,include_answers:true,include_comments:$('qa-comments').checked,max_seconds:120};if(mode==='qa-search')params.query=$('urls').value.trim();else params.ids=values;body={kind:'platform.discover',params};
  }else if(['bili-hot','tieba-hot','reddit-hot','reddit-qa'].includes(mode)){
    const params={platform:mode==='bili-hot'?'bili':mode==='tieba-hot'?'tieba':'reddit',mode:mode==='reddit-qa'?'qa':'hot',max_pages:Number($('max-pages').value),max_items:20};if(mode!=='tieba-hot')params.max_comments=20;
    if(mode==='reddit-hot')params.subreddit=values[0];
    if(mode==='reddit-qa')params.ids=values;
    body={kind:'platform.discover',params};
  }else if(['seed','map'].includes(mode)){
    if(values.length!==1){message('每个网址发现任务需要一个网站链接',true);return;}
    body={kind:'web.'+mode,params:{url:values[0],max_documents:Number($('max-pages').value),max_urls:100,max_seconds:60}};
    body.params.sources=mode==='map'?['robots','sitemap','homepage']:['robots','sitemap'];
    for(const source of ['cc','wayback','crt'])if($('discovery-'+source).checked&&!$('discovery-'+source).disabled)body.params.sources.push(source);
  }else if(['ocr','transcribe','vision'].includes(mode))body={kind:{ocr:'media.ocr',transcribe:'media.transcribe',vision:'media.vision'}[mode],params:{files:values,max_seconds:600}};
  else if(['image-generate','image-edit'].includes(mode)){
    body={kind:mode==='image-edit'?'creation.edit':'creation.generate',params:imageCreationParams(mode)};
  }
  else if(mode==='media')body={kind:'media.download',params:{urls:values,max_files:20,max_bytes:100000000}};
  else if(mode==='research'||mode==='web-search')body={kind:mode==='research'?'research.answer':'web.search',params:{query:$('urls').value.trim(),question:$('urls').value.trim(),limit:5,fetch_top:3,max_seconds:mode==='research'?600:180,...answerBackendParams(mode)}};
  else if(['web','web-extract','web-semantic'].includes(mode)){
    body={kind:'web.collect',params:{urls:values,max_pages:Number($('max-pages').value)}};
    body.params.stream=$('web-stream').checked;body.params.max_depth=Number($('web-depth').value);
    if($('web-topic').value.trim()){body.params.adaptive={query:$('web-topic').value.trim(),strategy:$('web-exploration').value};if($('web-exploration').value==='embedding'){body.params.max_seconds=600;body.params.adaptive.query_expansion=$('web-query-expansion').checked;}}
    if($('web-crawler').value){const name=$('web-crawler').value;body.params.crawler={name:name==='google_images'?'google_search':name,search_type:name==='google_images'?'image':'text'};body.params.max_seconds=600;}
    const engine=$('web-engine').value;if(engine){body.params.browser_type=engine==='patchright'?'chromium':engine;body.params.browser_provider=engine==='patchright'?'patchright':'playwright';}
    if($('web-downloads').checked&&!$('web-downloads').disabled)body.params.accept_downloads=true;
    if($('web-stitch').checked){body.params.screenshot=true;body.params.screenshot_mode='stitched';}
    if($('web-detect-blocking').checked)body.params.detect_blocking=true;
    if($('web-diagnostics').checked){body.params.capture_network_requests=true;body.params.capture_console_messages=true;if(!['firefox','webkit'].includes(engine))body.params.fetch_ssl_certificate=true;}
    if($('web-c4a').value.trim())body.params.c4a_script=$('web-c4a').value.trim();
    if($('web-chunking').value)body.params.chunking={type:$('web-chunking').value};
    if($('web-profile').value.trim()&&!$('web-profile').disabled)body.params.browser_profile=$('web-profile').value.trim();
    const proxyNames=$('web-proxy-names').value.split(/[,，]/).map(value=>value.trim()).filter(Boolean);if(proxyNames.length)body.params.proxy_policy={names:proxyNames,strategy:$('web-proxy-strategy').value};
    if($('web-ua-mode').value){body.params.user_agent_mode=$('web-ua-mode').value;if(body.params.user_agent_mode==='explicit')body.params.user_agent=$('web-ua-value').value.trim();}
    if($('web-adaptive').checked)body.params.dispatch={mode:'memory_adaptive'};
    if($('web-filter').value.trim()){body.params.llm_content_filter={mode:$('web-filter-mode').value,instruction:$('web-filter').value.trim(),max_chunks:4,max_records:20};body.params.max_seconds=600;}
    if($('web-schema').value.trim()){body.params.schema_inference={query:$('web-schema').value.trim()};body.params.max_seconds=600;}
    if($('web-tables').checked){body.params.llm_tables={max_tables:8,max_chunks:16,max_tokens:768};body.params.max_seconds=600;}
    if(mode==='web-semantic'){body.params.semantic_extraction={word_count_threshold:1,linkage_method:'average',max_dist:.4};body.params.max_seconds=600;}
    if(mode==='web-extract'){body.params.max_seconds=600;body.params.llm_extraction={instruction:$('extraction-instruction').value.trim()||'Extract the main facts and entities from this source.',max_chunks:4,max_records:20};}
  }
  else body={kind:{search:'search.local',semantic:'search.semantic',answer:'answer.local',model:'answer.model'}[mode],params:{query:$('urls').value.trim(),question:$('urls').value.trim(),limit:5,...(mode==='model'?{max_seconds:600,retrieval:$('model-retrieval').value,...answerBackendParams(mode)}:{})}};
  if((isPlatform()||Object.hasOwn(homepageModes,mode)||['web','web-extract','web-semantic','media','bili-hot','tieba-hot','reddit-hot','reddit-qa'].includes(mode))&&$('account-select').value)body.params.account=$('account-select').value;
  if(window.researchPlatformOptions)window.researchPlatformOptions(body);
  $('submit').disabled=true;message('正在创建任务…');
  try{const job=await api('jobs',{method:'POST',body:JSON.stringify(body)});selected=job.id;message('任务已开始');await refresh();}catch(error){message(error.message,true);}finally{$('submit').disabled=false;}
});
$('account-form').addEventListener('submit',async event=>{
  event.preventDefault();try{const job=await api('jobs',{method:'POST',body:JSON.stringify({kind:'account.login',params:{platform:$('account-platform').value,account:$('account-name').value.trim()}})});selected=job.id;message('正在打开平台登录窗口');await refresh();}catch(error){message(error.message,true);}
});
$('refresh').addEventListener('click',refresh);loadStatus().then(refresh);
setInterval(()=>{if(!document.hidden)refresh();},2500);
const bridgeButton=node('button','通过 Chrome 扩展导入');bridgeButton.type='button';$('account-form').append(bridgeButton);
const bridgePanel=node('div');bridgePanel.id='bridge-panel';$('account-form').after(bridgePanel);
bridgeButton.addEventListener('click',async()=>{
  try{
    const platform=$('account-platform').value,account=$('account-name').value.trim();
    if(!accounts.some(item=>item.platform===platform&&item.account===account))await api('accounts',{method:'POST',body:JSON.stringify({platform,account})});
    const pairing=await api('bridge/pairings',{method:'POST',body:JSON.stringify({platform,account})});
    bridgePanel.replaceChildren(node('p','配对码：'+pairing.code),node('p','在 Chrome 扩展管理页加载项目 bridge-extension 文件夹。打开 '+pairing.site+'，点击扩展并输入配对码；两分钟内有效。'));
    const revoke=node('button','撤销此次配对');revoke.type='button';revoke.addEventListener('click',async()=>{try{await api('bridge/pairings/'+pairing.id,{method:'DELETE'});bridgePanel.replaceChildren(node('p','配对已撤销。'));}catch(error){message(error.message,true);}});bridgePanel.append(revoke);
    await loadAccounts();
  }catch(error){message(error.message,true);}
});
