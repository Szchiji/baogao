import html
import json
from typing import Any

from app.crud import setting_get
from app.keyboards import report_template
from app.utils import parse_json


def report_to_html(report_row: dict) -> str:
    data = parse_json(report_row["data_json"], {})
    lines = [f"<h1>报告 #{report_row['id']}</h1>"]
    lines.append(f"<p>状态：{report_row['status']}</p>")
    lines.append(f"<p>用户：@{report_row['username'] or 'unknown'}</p>")
    lines.append("<ul>")
    for k, v in data.items():
        lines.append(f"<li><b>{k}</b>：{v}</li>")
    lines.append("</ul>")
    return "\n".join(lines)


_ADMIN_CSS = """
:root{--pri:#4f46e5;--pri-d:#4338ca;--pri-l:#ede9fe;--suc:#059669;--suc-l:#d1fae5;--dan:#dc2626;--dan-l:#fee2e2;--txt:#111827;--txt2:#6b7280;--txt3:#9ca3af;--bdr:#e5e7eb;--bg:#f9fafb;--card:#ffffff;--hov:#f3f4f6;--sb-bg:#1e1b4b;--sb-txt:#c7d2fe;--sh:0 1px 3px rgba(0,0,0,.08);--sh2:0 4px 12px rgba(0,0,0,.1);--r:8px;--r2:12px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;background:var(--bg);color:var(--txt);font-size:14px;line-height:1.5;min-height:100vh}
.layout{display:flex;min-height:100vh}
.sidebar{width:228px;flex-shrink:0;background:var(--sb-bg);display:flex;flex-direction:column;position:fixed;top:0;left:0;bottom:0;z-index:100;transition:transform .25s ease}
.sidebar-logo{padding:22px 18px 18px;border-bottom:1px solid rgba(255,255,255,.08)}
.sidebar-logo h1{font-size:.95rem;font-weight:700;color:#fff;line-height:1.4}
.sidebar-logo .subtitle{font-size:.72rem;color:var(--sb-txt);margin-top:2px;opacity:.65}
.sidebar-nav{flex:1;padding:10px 8px;overflow-y:auto}
.nav-group{font-size:.68rem;font-weight:700;color:rgba(255,255,255,.28);text-transform:uppercase;letter-spacing:.09em;padding:14px 12px 4px}
.nav-item{display:flex;align-items:center;gap:9px;padding:9px 12px;border-radius:6px;cursor:pointer;color:var(--sb-txt);font-size:.86rem;font-weight:500;border:none;background:none;width:100%;text-align:left;transition:all .15s;font-family:inherit;white-space:nowrap}
.nav-item:hover{background:rgba(255,255,255,.09);color:#fff}
.nav-item.active{background:var(--pri);color:#fff}
.nav-icon{font-size:.95rem;flex-shrink:0;width:18px;text-align:center}
.nav-badge{margin-left:auto;background:#ef4444;color:#fff;border-radius:10px;font-size:.68rem;font-weight:700;min-width:17px;height:17px;display:inline-flex;align-items:center;justify-content:center;padding:0 4px}
.sidebar-footer{padding:14px 8px;border-top:1px solid rgba(255,255,255,.08)}
.sidebar-footer a{display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:6px;color:var(--sb-txt);text-decoration:none;font-size:.84rem;transition:all .15s}
.sidebar-footer a:hover{background:rgba(255,255,255,.09);color:#fff}
.sidebar-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:99}
.sidebar-overlay.active{display:block}
.main{flex:1;margin-left:228px;min-height:100vh;display:flex;flex-direction:column}
.topbar{background:var(--card);border-bottom:1px solid var(--bdr);padding:0 28px;height:54px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50;box-shadow:var(--sh)}
.topbar-title{font-size:.95rem;font-weight:600;color:var(--txt)}
.hamburger{display:none;flex-direction:column;gap:4px;cursor:pointer;padding:8px;background:none;border:none}
.hamburger span{display:block;width:20px;height:2px;background:var(--txt2);border-radius:1px;transition:all .2s}
.content{flex:1;padding:28px}
.alert{display:flex;align-items:center;gap:10px;padding:12px 16px;border-radius:var(--r);margin-bottom:20px;font-size:.875rem;font-weight:500}
.alert-success{background:var(--suc-l);color:#065f46;border:1px solid #6ee7b7}
.tab-pane{display:none}
.tab-pane.active{display:block}
.section-title{font-size:.75rem;font-weight:700;color:var(--txt2);text-transform:uppercase;letter-spacing:.07em;margin-bottom:18px;padding-bottom:12px;border-bottom:1px solid var(--bdr)}
.card{background:var(--card);border-radius:var(--r2);box-shadow:var(--sh);border:1px solid var(--bdr);padding:24px;margin-bottom:20px}
.stats-row{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}
.stat-card{flex:1;min-width:130px;background:var(--card);border:1px solid var(--bdr);border-radius:var(--r2);padding:20px;box-shadow:var(--sh)}
.stat-val{font-size:2rem;font-weight:700;color:var(--pri);line-height:1}
.stat-lbl{font-size:.78rem;color:var(--txt3);margin-top:6px}
.field{margin-bottom:18px}
.field-row{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}
label{display:block;font-size:.78rem;font-weight:600;color:var(--txt2);margin-bottom:5px;text-transform:uppercase;letter-spacing:.04em}
.hint{font-size:.77rem;color:var(--txt3);margin-top:5px;line-height:1.5}
input[type=text],textarea,select{width:100%;padding:9px 11px;border:1.5px solid var(--bdr);border-radius:var(--r);font-size:.875rem;font-family:inherit;background:var(--card);color:var(--txt);transition:border-color .15s,box-shadow .15s;-webkit-appearance:none}
input[type=text]:focus,textarea:focus,select:focus{outline:none;border-color:var(--pri);box-shadow:0 0 0 3px rgba(79,70,229,.12)}
input[type=text][readonly]{background:var(--hov);color:var(--txt3);cursor:default}
textarea{resize:vertical;min-height:80px}
select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%236b7280' d='M6 8L0 0h12z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center;padding-right:30px;cursor:pointer}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border:none;border-radius:var(--r);cursor:pointer;font-size:.875rem;font-weight:500;font-family:inherit;transition:all .15s;text-decoration:none;white-space:nowrap;line-height:1.4}
.btn-primary{background:var(--pri);color:#fff;box-shadow:0 1px 2px rgba(79,70,229,.2)}
.btn-primary:hover{background:var(--pri-d);box-shadow:0 2px 6px rgba(79,70,229,.3)}
.btn-danger{background:var(--dan);color:#fff}
.btn-danger:hover{background:#b91c1c}
.btn-success{background:var(--suc);color:#fff}
.btn-success:hover{background:#047857}
.btn-secondary{background:var(--hov);color:var(--txt2);border:1px solid var(--bdr)}
.btn-secondary:hover{background:var(--bdr);color:var(--txt)}
.btn-sm{padding:5px 10px;font-size:.78rem}
.btn-add{display:flex;align-items:center;justify-content:center;gap:6px;background:var(--pri-l);color:var(--pri);border:1.5px dashed #a5b4fc;padding:8px 16px;width:100%;border-radius:var(--r);cursor:pointer;font-size:.84rem;font-weight:500;margin-top:8px;transition:all .15s;font-family:inherit}
.btn-add:hover{background:#ddd6fe;border-color:var(--pri)}
.editor-row{display:flex;gap:8px;align-items:center;margin-bottom:8px;padding:10px 13px;background:var(--bg);border:1.5px solid var(--bdr);border-radius:var(--r);transition:border-color .15s}
.editor-row:hover{border-color:#d1d5db}
.editor-row input,.editor-row select{flex:1;min-width:60px}
.tpl-field-card{background:var(--bg);border:1.5px solid var(--bdr);border-radius:var(--r);margin-bottom:10px;overflow:hidden;transition:border-color .15s}
.tpl-field-card:hover{border-color:#d1d5db}
.tpl-field-card .editor-row{background:transparent;border:none;border-radius:0;margin-bottom:0}
.save-bar{background:var(--card);border-top:1px solid var(--bdr);padding:16px 28px;display:flex;justify-content:flex-end;gap:10px;position:sticky;bottom:0;box-shadow:0 -1px 4px rgba(0,0,0,.06);margin-top:8px}
.table{width:100%;border-collapse:collapse;font-size:.875rem}
.table th,.table td{padding:11px 14px;text-align:left;border-bottom:1px solid var(--bdr)}
.table th{background:var(--bg);font-weight:600;color:var(--txt2);font-size:.74rem;text-transform:uppercase;letter-spacing:.06em}
.table tbody tr:hover td{background:#fafafa}
.table td input{padding:5px 8px;border:1.5px solid var(--bdr);border-radius:5px;font-size:.84rem;width:150px}
.muted{color:var(--txt3);font-style:italic}
.badge{display:inline-flex;align-items:center;justify-content:center;background:#ef4444;color:#fff;border-radius:10px;font-size:.7rem;font-weight:700;min-width:18px;height:18px;padding:0 5px;margin-left:4px;vertical-align:middle}
.rte-wrap{border:1.5px solid var(--bdr);border-radius:var(--r);overflow:hidden;background:var(--card)}
.rte-wrap:focus-within{border-color:var(--pri);box-shadow:0 0 0 3px rgba(79,70,229,.12)}
.rte-toolbar{display:flex;flex-wrap:wrap;gap:2px;padding:6px 8px;background:var(--bg);border-bottom:1px solid var(--bdr)}
.rte-btn{padding:4px 8px;border:1px solid transparent;border-radius:5px;background:none;cursor:pointer;font-size:.84rem;font-family:inherit;color:var(--txt2);transition:all .1s;line-height:1.4}
.rte-btn:hover{background:var(--bdr);border-color:#d1d5db;color:var(--txt)}
.rte-body{padding:10px 12px;min-height:80px;outline:none;font-size:.875rem;line-height:1.6;font-family:inherit;word-break:break-word}
.rte-body:empty:before{content:attr(data-ph);color:var(--txt3);pointer-events:none;display:block}
.rte-pills{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}
.rte-pill{padding:3px 10px;background:var(--pri-l);color:var(--pri);border:1px solid #a5b4fc;border-radius:12px;cursor:pointer;font-size:.77rem;font-weight:500;transition:all .15s;font-family:inherit}
.rte-pill:hover{background:#ddd6fe;border-color:var(--pri)}
@media(max-width:768px){.sidebar{transform:translateX(-100%)}.sidebar.open{transform:translateX(0)}.main{margin-left:0}.hamburger{display:flex}.field-row{grid-template-columns:1fr}.content{padding:16px}.topbar{padding:0 16px}.save-bar{padding:12px 16px}}
"""

_ADMIN_JS = """
(function(){
  var navItems=document.querySelectorAll('.nav-item');
  var tabPanes=document.querySelectorAll('.tab-pane');
  var saveBar=document.getElementById('settings-save-bar');
  var sidebar=document.querySelector('.sidebar');
  var overlay=document.querySelector('.sidebar-overlay');
  var hamburger=document.getElementById('hamburger');
  var topbarTitle=document.getElementById('topbar-title');
  var noSaveTabs=['pending','blacklist','broadcast'];
  function switchTab(tab){
    navItems.forEach(function(b){b.classList.remove('active');});
    tabPanes.forEach(function(p){p.classList.remove('active');});
    var pane=document.getElementById('pane-'+tab);
    if(pane) pane.classList.add('active');
    navItems.forEach(function(b){
      if(b.dataset.tab===tab){
        b.classList.add('active');
        if(topbarTitle){var lbl=b.querySelector('.nav-label');if(lbl)topbarTitle.textContent=lbl.textContent;}
      }
    });
    if(saveBar) saveBar.style.display=noSaveTabs.indexOf(tab)>=0?'none':'';
    if(tab==='review'&&_rteMap['push_template'])_rteMap['push_template'].refreshPills();
    if(tab==='broadcast'&&_rteMap['broadcast_text'])_rteMap['broadcast_text'].refreshPills();
    if(sidebar) sidebar.classList.remove('open');
    if(overlay) overlay.classList.remove('active');
  }
  navItems.forEach(function(btn){
    btn.addEventListener('click',function(){switchTab(btn.dataset.tab);});
  });
  if(hamburger){
    hamburger.addEventListener('click',function(){
      sidebar.classList.toggle('open');
      overlay.classList.toggle('active');
    });
  }
  if(overlay){
    overlay.addEventListener('click',function(){
      sidebar.classList.remove('open');
      overlay.classList.remove('active');
    });
  }

  // Start Buttons Editor
  var startBtnsData=__START_BUTTONS__;
  var startRows=document.getElementById('start-btn-rows');
  function makeStartRow(item){
    var row=document.createElement('div'); row.className='editor-row';
    var textIn=document.createElement('input');
    textIn.type='text'; textIn.placeholder='按钮文字'; textIn.value=item.text||'';
    textIn.dataset.field='text'; textIn.style.flex='1';
    var urlIn=document.createElement('input');
    urlIn.type='text'; urlIn.placeholder='链接 URL（https://...）'; urlIn.value=item.url||'';
    urlIn.dataset.field='url'; urlIn.style.flex='2';
    var rm=document.createElement('button');
    rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
    rm.addEventListener('click',function(){row.remove();});
    row.appendChild(textIn); row.appendChild(urlIn); row.appendChild(rm);
    return row;
  }
  startBtnsData.forEach(function(item){startRows.appendChild(makeStartRow(item));});
  document.getElementById('start-btn-add').addEventListener('click',function(){
    startRows.appendChild(makeStartRow({text:'',url:''}));
  });
  function serializeStartBtns(){
    var result=[];
    startRows.querySelectorAll('.editor-row').forEach(function(row){
      var text=row.querySelector('[data-field=text]').value.trim();
      var url=row.querySelector('[data-field=url]').value.trim();
      if(text&&url) result.push({text:text,url:url});
    });
    document.getElementById('start_buttons_json').value=JSON.stringify(result);
  }

  // Keyboard Buttons Editor
  var kbData=__KB_BUTTONS__;
  var kbRows=document.getElementById('kb-rows');
  var KB_ACTIONS=[
    {value:'write_report',label:'写报告（内置）'},
    {value:'search_help',label:'查阅报告（内置）'},
    {value:'contact',label:'联系管理员（内置）'},
    {value:'usage',label:'操作方式（内置）'},
    {value:'text',label:'自定义回复文本'}
  ];
  function makeKbRow(item){
    var row=document.createElement('div'); row.className='editor-row';
    var textIn=document.createElement('input');
    textIn.type='text'; textIn.placeholder='按钮文字'; textIn.value=item.text||'';
    textIn.dataset.field='text';
    var sel=document.createElement('select');
    sel.dataset.field='action'; sel.style.flex='none'; sel.style.width='180px';
    KB_ACTIONS.forEach(function(a){
      var opt=document.createElement('option');
      opt.value=a.value; opt.textContent=a.label;
      if(item.action===a.value) opt.selected=true;
      sel.appendChild(opt);
    });
    var valIn=document.createElement('input');
    valIn.type='text'; valIn.placeholder='回复内容'; valIn.value=item.value||'';
    valIn.dataset.field='value';
    valIn.style.display=(item.action==='text')?'':'none';
    sel.addEventListener('change',function(){
      valIn.style.display=sel.value==='text'?'':'none';
    });
    var rowIn=document.createElement('input');
    rowIn.type='text'; rowIn.placeholder='行号'; rowIn.value=item.row||'';
    rowIn.dataset.field='row'; rowIn.style.width='50px'; rowIn.style.flex='none';
    rowIn.title='相同行号的按钮同行显示，留空则独占一行';
    var rm=document.createElement('button');
    rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
    rm.addEventListener('click',function(){row.remove();});
    row.appendChild(textIn); row.appendChild(sel); row.appendChild(valIn); row.appendChild(rowIn); row.appendChild(rm);
    return row;
  }
  kbData.forEach(function(item){kbRows.appendChild(makeKbRow(item));});
  document.getElementById('kb-add').addEventListener('click',function(){
    kbRows.appendChild(makeKbRow({text:'',action:'write_report',value:''}));
  });
  function serializeKb(){
    var result=[];
    kbRows.querySelectorAll('.editor-row').forEach(function(row){
      var text=row.querySelector('[data-field=text]').value.trim();
      var action=row.querySelector('[data-field=action]').value;
      var value=row.querySelector('[data-field=value]').value.trim();
      var rowNum=row.querySelector('[data-field=row]').value.trim();
      if(text){
        var item={text:text,action:action};
        if(action==='text'&&value) item.value=value;
        if(rowNum) item.row=rowNum;
        result.push(item);
      }
    });
    document.getElementById('keyboard_buttons_json').value=JSON.stringify(result);
  }

  // Report Template Editor
  var tplData=__TEMPLATE__;
  var tplFieldsEl=document.getElementById('template-fields');
  var tplNameIn=document.getElementById('template-name');
  tplNameIn.value=tplData.name||'';
  function makeTplRow(field){
    var card=document.createElement('div'); card.className='tpl-field-card';
    // Row 1: key, label, type, required, remove
    var row1=document.createElement('div'); row1.className='editor-row'; row1.style.marginBottom='4px';
    var keyIn=document.createElement('input');
    keyIn.type='text'; keyIn.placeholder='英文标识（如 title）'; keyIn.value=field.key||'';
    keyIn.dataset.field='key'; keyIn.style.flex='1';
    var labelIn=document.createElement('input');
    labelIn.type='text'; labelIn.placeholder='显示名称（如 标题）'; labelIn.value=field.label||'';
    labelIn.dataset.field='label'; labelIn.style.flex='1';
    var typeSel=document.createElement('select');
    typeSel.dataset.field='type'; typeSel.style.flex='none'; typeSel.style.width='80px';
    [{value:'text',label:'文本'},{value:'photo',label:'图片'}].forEach(function(o){
      var opt=document.createElement('option');
      opt.value=o.value; opt.textContent=o.label;
      if((field.type||'text')===o.value) opt.selected=true;
      typeSel.appendChild(opt);
    });
    var reqLabel=document.createElement('label');
    reqLabel.style.cssText='display:flex;align-items:center;gap:4px;font-weight:normal;font-size:.85rem;white-space:nowrap;flex:none;text-transform:none;letter-spacing:0;color:#475569;';
    var reqCheck=document.createElement('input');
    reqCheck.type='checkbox'; reqCheck.dataset.field='required'; reqCheck.style.margin='0';
    reqCheck.checked=(field.required!==false);
    reqLabel.appendChild(reqCheck); reqLabel.appendChild(document.createTextNode('必填'));
    var rm=document.createElement('button');
    rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
    rm.addEventListener('click',function(){card.remove();});
    row1.appendChild(keyIn); row1.appendChild(labelIn); row1.appendChild(typeSel); row1.appendChild(reqLabel); row1.appendChild(rm);
    // Row 2: hint input
    var row2=document.createElement('div'); row2.style.cssText='padding:0 12px 10px;';
    var hintIn=document.createElement('input');
    hintIn.type='text'; hintIn.placeholder='字段说明（选填）：例如"请填写今日工作摘要"，显示给用户作为填写提示';
    hintIn.value=field.hint||''; hintIn.dataset.field='hint'; hintIn.style.width='100%';
    row2.appendChild(hintIn);
    card.appendChild(row1); card.appendChild(row2);
    return card;
  }
  (tplData.fields||[]).forEach(function(f){tplFieldsEl.appendChild(makeTplRow(f));});
  document.getElementById('template-add').addEventListener('click',function(){
    tplFieldsEl.appendChild(makeTplRow({key:'',label:'',hint:'',required:true,type:'text'}));
  });
  function serializeTemplate(){
    var fields=[];
    tplFieldsEl.querySelectorAll('.tpl-field-card').forEach(function(card){
      var key=card.querySelector('[data-field=key]').value.trim();
      var label=card.querySelector('[data-field=label]').value.trim();
      var hint=card.querySelector('[data-field=hint]').value.trim();
      var type=card.querySelector('[data-field=type]').value;
      var required=card.querySelector('[data-field=required]').checked;
      if(key&&label) fields.push({key:key,label:label,hint:hint,required:required,type:type});
    });
    var tpl={name:tplNameIn.value.trim()||'模板',fields:fields};
    document.getElementById('report_template_json').value=JSON.stringify(tpl);
  }

  document.getElementById('settings-form').addEventListener('submit',function(){
    Object.keys(_rteMap).forEach(function(k){if(_rteMap[k])_rteMap[k].sync();});
    serializeStartBtns();
    serializeKb();
    serializeTemplate();
    serializePushFields();
  });

  // Push Detail Fields Editor
  var pushDetailFieldsData=__PUSH_DETAIL_FIELDS__;
  var pushFieldsList=document.getElementById('push-detail-fields-list');
  function getTplTextFields(){
    var fields=[];
    tplFieldsEl.querySelectorAll('.tpl-field-card').forEach(function(card){
      var key=card.querySelector('[data-field=key]').value.trim();
      var label=card.querySelector('[data-field=label]').value.trim();
      var type=card.querySelector('[data-field=type]').value;
      if(key&&label&&type!=='photo') fields.push({key:key,label:label});
    });
    return fields;
  }
  function makePushFieldRow(key,label){
    var row=document.createElement('div'); row.className='editor-row'; row.dataset.key=key;
    var span=document.createElement('span');
    span.textContent=(label||key)+' ('+key+')'; span.style.flex='1';
    var up=document.createElement('button');
    up.type='button'; up.textContent='↑'; up.className='btn btn-sm';
    up.style.cssText='padding:3px 8px;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:4px;cursor:pointer;flex:none;';
    up.addEventListener('click',function(){var prev=row.previousElementSibling;if(prev)pushFieldsList.insertBefore(row,prev);});
    var down=document.createElement('button');
    down.type='button'; down.textContent='↓'; down.className='btn btn-sm';
    down.style.cssText='padding:3px 8px;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:4px;cursor:pointer;flex:none;';
    down.addEventListener('click',function(){var next=row.nextElementSibling;if(next)pushFieldsList.insertBefore(next,row);});
    var rm=document.createElement('button');
    rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
    rm.addEventListener('click',function(){row.remove();renderPushFieldsAddArea();});
    row.appendChild(span); row.appendChild(up); row.appendChild(down); row.appendChild(rm);
    return row;
  }
  function renderPushFieldsAddArea(){
    var addArea=document.getElementById('push-fields-add-area');
    addArea.innerHTML='';
    var existingKeys={};
    pushFieldsList.querySelectorAll('.editor-row[data-key]').forEach(function(r){existingKeys[r.dataset.key]=true;});
    getTplTextFields().forEach(function(f){
      if(!existingKeys[f.key]){
        var btn=document.createElement('button');
        btn.type='button'; btn.textContent='＋ '+f.label+' ('+f.key+')';
        btn.className='btn-add'; btn.style.marginTop='4px';
        btn.addEventListener('click',function(){
          pushFieldsList.appendChild(makePushFieldRow(f.key,f.label));
          renderPushFieldsAddArea();
        });
        addArea.appendChild(btn);
      }
    });
  }
  function initPushFields(){
    pushFieldsList.innerHTML='';
    var tplFields=getTplTextFields();
    var labelMap={};
    tplFields.forEach(function(f){labelMap[f.key]=f.label;});
    var initKeys=pushDetailFieldsData.length>0?pushDetailFieldsData:tplFields.map(function(f){return f.key;});
    initKeys.forEach(function(k){
      if(labelMap[k]) pushFieldsList.appendChild(makePushFieldRow(k,labelMap[k]));
    });
    renderPushFieldsAddArea();
  }
  function serializePushFields(){
    var result=[];
    pushFieldsList.querySelectorAll('.editor-row[data-key]').forEach(function(row){
      result.push(row.dataset.key);
    });
    document.getElementById('push_detail_fields_json').value=JSON.stringify(result);
  }
  initPushFields();

  // Rich Text Editor
  function serializeRTENode(node){
    var out='';
    node.childNodes.forEach(function(n){
      if(n.nodeType===3){
        out+=n.textContent.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      } else if(n.nodeType===1){
        var t=n.tagName.toLowerCase();
        var inner=serializeRTENode(n);
        if(t==='b'||t==='strong') out+='<b>'+inner+'</b>';
        else if(t==='i'||t==='em') out+='<i>'+inner+'</i>';
        else if(t==='u') out+='<u>'+inner+'</u>';
        else if(t==='s'||t==='strike'||t==='del') out+='<s>'+inner+'</s>';
        else if(t==='code') out+='<code>'+inner+'</code>';
        else if(t==='a'){var href=(n.getAttribute('href')||'').replace(/"/g,'&quot;');out+='<a href="'+href+'">'+inner+'</a>';}
        else if(t==='br') out+='\\n';
        else if(t==='div'||t==='p') out+=(inner||'')+'\\n';
        else out+=inner;
      }
    });
    return out;
  }
  var _rteMap={};
  function RichTextEditor(ta,getPills){
    var self=this; self._ta=ta; self._getPills=getPills||null; self._pd=null;
    var wrap=document.createElement('div'); wrap.className='rte-wrap';
    ta.parentNode.insertBefore(wrap,ta); ta.style.display='none';
    if(getPills){var pd=document.createElement('div');pd.className='rte-pills';wrap.appendChild(pd);self._pd=pd;}
    var tb=document.createElement('div'); tb.className='rte-toolbar'; wrap.appendChild(tb);
    var body=document.createElement('div'); body.className='rte-body'; body.contentEditable='true';
    body.setAttribute('data-ph',ta.getAttribute('placeholder')||'输入内容…');
    var existing=ta.value; if(existing) body.innerHTML=existing.replace(/\\n/g,'<br>');
    wrap.appendChild(body); self._body=body;
    var tools=[
      {cmd:'bold',html:'<b>B</b>',title:'粗体'},
      {cmd:'italic',html:'<i>I</i>',title:'斜体'},
      {cmd:'underline',html:'<u>U</u>',title:'下划线'},
      {cmd:'strikeThrough',html:'<s>S</s>',title:'删除线'},
      {cmd:'code',html:'<code style="font-size:.8rem">&lt;/&gt;</code>',title:'代码'},
      {cmd:'link',html:'🔗',title:'添加链接'},
      {cmd:'unlink',html:'🔗✕',title:'移除链接'},
      {cmd:'undo',html:'↩',title:'撤销'},
      {cmd:'redo',html:'↪',title:'重做'}
    ];
    tools.forEach(function(t){
      var btn=document.createElement('button'); btn.type='button';
      btn.innerHTML=t.html; btn.title=t.title; btn.className='rte-btn';
      btn.addEventListener('mousedown',function(e){
        e.preventDefault(); body.focus();
        if(t.cmd==='code'){
          var sel=window.getSelection();
          if(sel&&sel.rangeCount>0&&!sel.isCollapsed){
            var range=sel.getRangeAt(0);
            var codeEl=document.createElement('code');
            try{range.surroundContents(codeEl);}catch(ex){var et=range.toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');document.execCommand('insertHTML',false,'<code>'+et+'</code>');}
          } else {document.execCommand('insertHTML',false,'<code></code>');}
        } else if(t.cmd==='link'){
          var sel=window.getSelection(); var st=sel?sel.toString():'';
          var url=prompt('输入链接地址（https://...）','');
          if(url){
            if(st){document.execCommand('createLink',false,url);}
            else{var su=url.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');document.execCommand('insertHTML',false,'<a href="'+su+'">'+su+'</a>');}
          }
        } else if(t.cmd==='unlink'){document.execCommand('unlink');}
        else if(t.cmd==='undo'){document.execCommand('undo');}
        else if(t.cmd==='redo'){document.execCommand('redo');}
        else{document.execCommand(t.cmd);}
      });
      tb.appendChild(btn);
    });
    self.sync=function(){var raw=serializeRTENode(body);self._ta.value=raw.replace(/\\n+$/,'');};
    self.refreshPills=function(){
      if(!self._pd||!self._getPills)return;
      var pills=self._getPills(); self._pd.innerHTML='';
      pills.forEach(function(p){
        var btn=document.createElement('button'); btn.type='button'; btn.className='rte-pill';
        btn.textContent=p.label; btn.title='插入: '+p.insert;
        btn.addEventListener('click',function(){body.focus();document.execCommand('insertText',false,p.insert);});
        self._pd.appendChild(btn);
      });
    };
  }
  function getPushTemplatePills(){
    var pills=[{label:'报告ID',insert:'{id}'},{label:'用户名',insert:'{username}'},{label:'推送详情',insert:'{detail}'},{label:'报告链接',insert:'{link}'}];
    getTplTextFields().forEach(function(f){pills.push({label:f.label,insert:'{'+f.key+'}'});});
    return pills;
  }
  function getBroadcastPills(){
    var pills=[];
    getTplTextFields().forEach(function(f){pills.push({label:f.label,insert:'{'+f.key+'}'});});
    return pills;
  }
  ['start_text','search_help_text','contact_text','usage_text'].forEach(function(name){
    var ta=document.querySelector('[name="'+name+'"]');
    if(ta) _rteMap[name]=new RichTextEditor(ta,null);
  });
  var ptTa=document.querySelector('[name="push_template"]');
  if(ptTa){_rteMap['push_template']=new RichTextEditor(ptTa,getPushTemplatePills);_rteMap['push_template'].refreshPills();}
  var btTa=document.querySelector('[name="broadcast_text"]');
  if(btTa){_rteMap['broadcast_text']=new RichTextEditor(btTa,getBroadcastPills);_rteMap['broadcast_text'].refreshPills();}

  // Broadcast Buttons Editor
  var broadcastBtnsRows=document.getElementById('broadcast-btn-rows');
  if(broadcastBtnsRows){
    function makeBroadcastRow(item){
      var row=document.createElement('div'); row.className='editor-row';
      var textIn=document.createElement('input');
      textIn.type='text'; textIn.placeholder='按钮文字'; textIn.value=item.text||'';
      textIn.dataset.field='text'; textIn.style.flex='1';
      var urlIn=document.createElement('input');
      urlIn.type='text'; urlIn.placeholder='链接 URL（https://...）'; urlIn.value=item.url||'';
      urlIn.dataset.field='url'; urlIn.style.flex='2';
      var rm=document.createElement('button');
      rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
      rm.addEventListener('click',function(){row.remove();});
      row.appendChild(textIn); row.appendChild(urlIn); row.appendChild(rm);
      return row;
    }
    document.getElementById('broadcast-btn-add').addEventListener('click',function(){
      broadcastBtnsRows.appendChild(makeBroadcastRow({text:'',url:''}));
    });
    function serializeBroadcastBtns(){
      var result=[];
      broadcastBtnsRows.querySelectorAll('.editor-row').forEach(function(row){
        var text=row.querySelector('[data-field=text]').value.trim();
        var url=row.querySelector('[data-field=url]').value.trim();
        if(text&&url) result.push({text:text,url:url});
      });
      document.getElementById('broadcast_buttons_json').value=JSON.stringify(result);
    }
    document.getElementById('broadcast-form').addEventListener('submit',function(){
      if(_rteMap['broadcast_text'])_rteMap['broadcast_text'].sync();
      serializeBroadcastBtns();
      return confirm('确认向所有用户发送广播？');
    });
  }
})();
"""


def _render_report_content_for_admin(data_json: str, tpl_fields: list[dict[str, Any]]) -> str:
    """Return a short HTML snippet showing all field values of a report for admin review."""
    data = parse_json(data_json, {})
    if not data:
        return "<em style='color:#94a3b8'>（无内容）</em>"
    field_labels = {f["key"]: f["label"] for f in tpl_fields}
    field_types = {f["key"]: f.get("type", "text") for f in tpl_fields}
    parts = []
    for k, v in data.items():
        label = html.escape(field_labels.get(k, k))
        if field_types.get(k, "text") == "photo":
            parts.append(f"<b>{label}</b>：📷（图片，请在Telegram通知中查看）")
        else:
            display = html.escape(str(v)[:300])
            parts.append(f"<b>{label}</b>：{display}")
    return "<br>".join(parts) if parts else "<em style='color:#94a3b8'>（无内容）</em>"


def build_admin_html(settings_map: dict[str, str], pending_reports: list[dict] | None = None, saved: bool = False, user_count: int = 0, db_path: str = "", blacklist: list[dict] | None = None) -> str:
    def e(key: str) -> str:
        return html.escape(settings_map.get(key, ""))

    def safe_js(key: str, fallback: Any) -> str:
        raw = settings_map.get(key, "")
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = fallback
        return (
            json.dumps(parsed, ensure_ascii=False)
            .replace("</", r"<\/")
            .replace("\u2028", r"\u2028")
            .replace("\u2029", r"\u2029")
        )

    start_buttons_js = safe_js("start_buttons_json", [])
    kb_buttons_js = safe_js("keyboard_buttons_json", [])
    template_js = safe_js("report_template_json", {"name": "", "fields": []})
    push_detail_fields_js = safe_js("push_detail_fields_json", [])

    js = (
        _ADMIN_JS
        .replace("__START_BUTTONS__", start_buttons_js)
        .replace("__KB_BUTTONS__", kb_buttons_js)
        .replace("__TEMPLATE__", template_js)
        .replace("__PUSH_DETAIL_FIELDS__", push_detail_fields_js)
    )

    pending_count = len(pending_reports) if pending_reports else 0
    pending_badge = f'<span class="badge">{pending_count}</span>' if pending_count > 0 else ""

    if pending_reports:
        tpl_fields = report_template()["fields"]
        rows_html = ""
        for r in pending_reports:
            content_html = _render_report_content_for_admin(r.get("data_json", "{}"), tpl_fields)
            rows_html += (
                "<tr>"
                f"<td>#{r['id']}</td>"
                f"<td>@{html.escape(r['username'] or 'unknown')}</td>"
                f"<td style='white-space:nowrap'>{html.escape(str(r['created_at'])[:19])}</td>"
                f"<td style='max-width:320px;word-break:break-word;font-size:.85rem;line-height:1.6'>{content_html}</td>"
                "<td style='white-space:nowrap;vertical-align:middle'>"
                f"<form method='post' action='/admin/approve/{r['id']}' style='display:block;margin-bottom:6px'>"
                "<button class='btn btn-success btn-sm' type='submit'>✅ 通过</button></form>"
                f"<form method='post' action='/admin/reject/{r['id']}' style='display:flex;gap:4px;align-items:center'>"
                "<input name='reason' placeholder='驳回原因' style='width:110px;padding:4px 6px;border:1px solid #cbd5e1;border-radius:4px;font-size:.8rem'>"
                "<button class='btn btn-danger btn-sm' type='submit'>❌ 驳回</button></form>"
                "</td></tr>"
            )
        pending_html = (
            "<div style='margin-bottom:12px'>"
            "<a href='/admin#tab=pending' onclick='location.reload();return false;' style='font-size:.85rem;color:#2563eb;text-decoration:none'>🔄 刷新列表</a>"
            "</div>"
            "<table class='table'><thead><tr>"
            "<th>ID</th><th>用户</th><th>提交时间</th><th>报告内容</th><th>操作</th>"
            "</tr></thead><tbody>" + rows_html + "</tbody></table>"
        )
    else:
        pending_html = "<p class='muted'>暂无待审核报告。</p>"

    # Build blacklist HTML
    if blacklist:
        bl_rows = ""
        for entry in blacklist:
            uid = html.escape(str(entry.get("user_id", "")))
            uname = html.escape(entry.get("username") or "")
            reason = html.escape(entry.get("reason") or "")
            added = html.escape(str(entry.get("added_at", ""))[:19])
            bl_rows += (
                "<tr>"
                f"<td>{uid}</td>"
                f"<td>{'@' + uname if uname else '<em style=\"color:#94a3b8\">未知</em>'}</td>"
                f"<td>{reason}</td>"
                f"<td style='white-space:nowrap'>{added}</td>"
                "<td>"
                f"<form method='post' action='/admin/blacklist/unban/{entry['user_id']}'>"
                "<button class='btn btn-success btn-sm' type='submit'>✅ 解除</button></form>"
                "</td></tr>"
            )
        blacklist_html = (
            "<table class='table'><thead><tr>"
            "<th>用户ID</th><th>用户名</th><th>原因</th><th>封禁时间</th><th>操作</th>"
            "</tr></thead><tbody>" + bl_rows + "</tbody></table>"
        )
    else:
        blacklist_html = "<p class='muted'>黑名单为空。</p>"

    saved_banner = "<div class='alert alert-success'>✅ 配置已保存成功！</div>" if saved else ""

    media_types = [("", "无"), ("photo", "图片"), ("video", "视频")]
    current_media_type = settings_map.get("start_media_type", "").strip().lower()
    media_type_options = "".join(
        f"<option value='{v}'{' selected' if v == current_media_type else ''}>{label}</option>"
        for v, label in media_types
    )

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>报告机器人管理后台</title>
<style>{_ADMIN_CSS}</style>
</head>
<body>
<div class="sidebar-overlay"></div>
<div class="layout">

<aside class="sidebar">
  <div class="sidebar-logo">
    <h1>📋 报告机器人</h1>
    <div class="subtitle">管理后台</div>
  </div>
  <nav class="sidebar-nav">
    <div class="nav-group">内容管理</div>
    <button type="button" class="nav-item active" data-tab="basic"><span class="nav-icon">⚙️</span><span class="nav-label">基本设置</span></button>
    <button type="button" class="nav-item" data-tab="welcome"><span class="nav-icon">👋</span><span class="nav-label">欢迎消息</span></button>
    <button type="button" class="nav-item" data-tab="keyboard"><span class="nav-icon">⌨️</span><span class="nav-label">底部菜单</span></button>
    <button type="button" class="nav-item" data-tab="template"><span class="nav-icon">📝</span><span class="nav-label">报告模板</span></button>
    <button type="button" class="nav-item" data-tab="texts"><span class="nav-icon">💬</span><span class="nav-label">文本配置</span></button>
    <button type="button" class="nav-item" data-tab="review"><span class="nav-icon">🔍</span><span class="nav-label">审核设置</span></button>
    <div class="nav-group">操作</div>
    <button type="button" class="nav-item" data-tab="pending"><span class="nav-icon">⏳</span><span class="nav-label">待审核报告</span>{"<span class='nav-badge'>" + str(pending_count) + "</span>" if pending_count > 0 else ""}</button>
    <button type="button" class="nav-item" data-tab="blacklist"><span class="nav-icon">🚫</span><span class="nav-label">黑名单</span></button>
    <button type="button" class="nav-item" data-tab="broadcast"><span class="nav-icon">📢</span><span class="nav-label">广播发送</span></button>
  </nav>
  <div class="sidebar-footer">
    <a href="/admin/logout">🚪 退出登录</a>
  </div>
</aside>

<div class="main">
  <div class="topbar">
    <button class="hamburger" id="hamburger" aria-label="菜单">
      <span></span><span></span><span></span>
    </button>
    <span class="topbar-title" id="topbar-title">基本设置</span>
    <span style="font-size:.8rem;color:var(--txt3)">{user_count} 位用户</span>
  </div>

  <div class="content">
    {saved_banner}

    <form id="settings-form" method="post" action="/admin/save">

    <div id="pane-basic" class="tab-pane active">
      <p class="section-title">基本设置</p>
      <div class="card">
        <div class="field-row">
          <div class="field">
            <label>强制订阅频道</label>
            <input type="text" name="force_sub_channel" value="{e('force_sub_channel')}" placeholder="@频道用户名">
            <div class="hint">填 @用户名，用户须先订阅该频道才能使用机器人（留空则不限制）</div>
          </div>
          <div class="field">
            <label>报告推送频道</label>
            <input type="text" name="push_channel" value="{e('push_channel')}" placeholder="@频道用户名">
            <div class="hint">审核通过的报告自动推送到该频道（留空则不推送）</div>
          </div>
        </div>
        <div class="field">
          <label>报告链接基地址</label>
          <input type="text" name="report_link_base" value="{e('report_link_base')}" placeholder="https://yourdomain.com">
          <div class="hint">报告查询结果显示链接的前缀，链接格式为：域名/reports/ID（留空则仅显示报告 ID）；当推送到频道时会自动使用频道消息链接，无需另行配置</div>
        </div>
        <div class="field">
          <label>数据库</label>
          <input type="text" value="PostgreSQL (DATABASE_URL)" readonly>
          <div class="hint">数据库使用 PostgreSQL，数据持久化存储，重新部署不会丢失。请确保在平台环境变量中设置 <code>DATABASE_URL</code>。</div>
        </div>
        <div class="field">
          <label>配置导出 / 导入</label>
          <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-start">
            <a href="/admin/export-settings" class="btn btn-secondary" style="text-decoration:none">⬇️ 导出配置 JSON</a>
            <div style="display:flex;gap:6px;align-items:flex-start;flex-wrap:wrap">
              <textarea name="settings_json" rows="3" placeholder="粘贴之前导出的配置 JSON..." style="width:300px;min-width:200px;font-size:.84rem;resize:vertical" form="import-settings-form"></textarea>
              <button type="submit" class="btn btn-success" onclick="return confirm('导入将覆盖现有配置，确认吗？')" form="import-settings-form">⬆️ 导入配置</button>
            </div>
          </div>
          <div class="hint" style="margin-top:6px">可将当前配置导出为 JSON 文件保存备份；重新部署后可导入恢复设置。</div>
        </div>
      </div>
    </div>

    <div id="pane-welcome" class="tab-pane">
      <p class="section-title">欢迎消息（/start 命令）</p>
      <div class="card">
        <div class="field">
          <label>/start 欢迎文本</label>
          <textarea name="start_text" rows="4">{e('start_text')}</textarea>
          <div class="hint">使用工具栏进行格式化；支持 Telegram HTML：加粗、斜体、下划线、链接等</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>媒体类型</label>
            <select name="start_media_type">
              {media_type_options}
            </select>
            <div class="hint">选择后需在右侧填写对应的媒体 URL</div>
          </div>
          <div class="field">
            <label>媒体 URL</label>
            <input type="text" name="start_media_url" value="{e('start_media_url')}" placeholder="https://...">
            <div class="hint">图片或视频的直链地址</div>
          </div>
        </div>
        <div class="field">
          <label>欢迎消息内联按钮</label>
          <div class="hint" style="margin-bottom:8px">显示在欢迎文字下方的按钮，点击后跳转链接</div>
          <div id="start-btn-rows"></div>
          <button type="button" class="btn-add" id="start-btn-add">＋ 添加按钮</button>
          <input type="hidden" name="start_buttons_json" id="start_buttons_json">
        </div>
      </div>
    </div>

    <div id="pane-keyboard" class="tab-pane">
      <p class="section-title">底部快捷键盘</p>
      <div class="card">
        <div class="hint" style="margin-bottom:14px">配置用户输入框下方的快捷按钮。可绑定内置功能，也可自定义回复内容。"行号"相同的按钮将显示在同一行（留空则独占一行）。</div>
        <div id="kb-rows"></div>
        <button type="button" class="btn-add" id="kb-add">＋ 添加按钮</button>
        <input type="hidden" name="keyboard_buttons_json" id="keyboard_buttons_json">
      </div>
    </div>

    <div id="pane-template" class="tab-pane">
      <p class="section-title">报告填写模板</p>
      <div class="card">
        <div class="field">
          <label>模板名称</label>
          <input type="text" id="template-name" placeholder="例如：日报">
          <input type="hidden" name="report_template_json" id="report_template_json">
        </div>
        <div class="field">
          <label>模板字段</label>
          <div class="hint" style="margin-bottom:10px">每个字段可设置：英文标识（键名）、显示名称、类型（文本/图片）、是否必填、字段说明（提示用户如何填写）</div>
          <div id="template-fields"></div>
          <button type="button" class="btn-add" id="template-add">＋ 添加字段</button>
        </div>
      </div>
    </div>

    <div id="pane-texts" class="tab-pane">
      <p class="section-title">功能文本配置</p>
      <div class="card">
        <div class="field">
          <label>查阅报告 — 帮助文本</label>
          <textarea name="search_help_text" rows="3">{e('search_help_text')}</textarea>
          <div class="hint">用户点击「查阅报告」后显示的提示，说明如何使用 @用户名 或 #标签 搜索</div>
        </div>
        <div class="field">
          <label>联系管理员 — 文本</label>
          <textarea name="contact_text" rows="3">{e('contact_text')}</textarea>
        </div>
        <div class="field">
          <label>操作方式 — 说明文本</label>
          <textarea name="usage_text" rows="5">{e('usage_text')}</textarea>
        </div>
      </div>
    </div>

    <div id="pane-review" class="tab-pane">
      <p class="section-title">审核反馈通知</p>
      <div class="card">
        <div class="field">
          <label>审核通过 — 通知模板</label>
          <input type="text" name="review_approved_template" value="{e('review_approved_template')}">
          <div class="hint">使用 {{id}} 表示报告编号，{{link}} 表示报告链接，例如：✅ 报告 #{{id}} 审核通过。{{link}}</div>
        </div>
        <div class="field">
          <label>审核驳回 — 通知模板</label>
          <input type="text" name="review_rejected_template" value="{e('review_rejected_template')}">
          <div class="hint">使用 {{id}} 表示编号，{{reason}} 表示驳回原因，例如：❌ 报告 #{{id}} 未通过：{{reason}}</div>
        </div>
        <div class="field">
          <label>推送频道 — 推送模板</label>
          <textarea name="push_template" rows="4">{e('push_template')}</textarea>
          <div class="hint">支持占位符：{{id}} 报告编号、{{username}} 用户名、{{detail}} 报告字段内容、{{link}} 报告链接；点击上方字段按钮快速插入。<br>还可直接使用字段键名，如模板含 <code>title</code> 字段则可用 {{{{title}}}}（前后各两个大括号）。</div>
        </div>
        <div class="field">
          <label>推送详情字段 — 顺序与选择</label>
          <div class="hint" style="margin-bottom:8px">拖动排序或点击 ↑↓ 调整字段在 {{{{detail}}}} 中的显示顺序；点击 ✕ 从推送中排除该字段。留空则默认包含全部文本字段。</div>
          <div id="push-detail-fields-list"></div>
          <div id="push-fields-add-area" style="margin-top:8px"></div>
          <input type="hidden" name="push_detail_fields_json" id="push_detail_fields_json">
        </div>
      </div>
    </div>

    <div class="save-bar" id="settings-save-bar">
      <button type="submit" class="btn btn-primary">💾 保存配置</button>
    </div>

    </form>
    <form id="import-settings-form" method="post" action="/admin/import-settings"></form>

    <div id="pane-pending" class="tab-pane">
      <p class="section-title">待审核报告（{pending_count} 条）</p>
      <div class="card" style="padding:0;overflow:hidden">
        <div style="padding:14px 18px;border-bottom:1px solid var(--bdr);display:flex;align-items:center;justify-content:space-between">
          <span style="font-size:.85rem;color:var(--txt2)">共 {pending_count} 条待审核</span>
          <a href="/admin#tab=pending" onclick="location.reload();return false;" style="font-size:.84rem;color:var(--pri);text-decoration:none">🔄 刷新</a>
        </div>
        <div style="overflow-x:auto">{pending_html}</div>
      </div>
    </div>

    <div id="pane-blacklist" class="tab-pane">
      <p class="section-title">黑名单管理</p>
      <div class="card">
        <form method="post" action="/admin/blacklist/ban" style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:20px;padding-bottom:18px;border-bottom:1px solid var(--bdr)">
          <div>
            <label>用户 ID</label>
            <input type="text" name="user_id" placeholder="数字用户ID" style="width:140px">
          </div>
          <div>
            <label>原因（可选）</label>
            <input type="text" name="reason" placeholder="限制原因" style="width:200px">
          </div>
          <button type="submit" class="btn btn-danger" style="margin-bottom:1px">🚫 加入黑名单</button>
        </form>
        <div style="overflow-x:auto">{blacklist_html}</div>
      </div>
    </div>

    <div id="pane-broadcast" class="tab-pane">
      <p class="section-title">广播发送</p>
      <div class="card">
        <div style="margin-bottom:16px;padding:12px 16px;background:var(--pri-l);border-radius:var(--r);font-size:.85rem;color:#3730a3;border:1px solid #a5b4fc">
          📊 共 <b>{user_count}</b> 位用户曾使用机器人
        </div>
        <form id="broadcast-form" method="post" action="/admin/broadcast">
          <div class="field">
            <label>广播文本</label>
            <textarea name="broadcast_text" rows="5" placeholder="使用工具栏格式化文字；点击字段按钮快速插入模板字段内容"></textarea>
          </div>
          <div class="field-row">
            <div class="field">
              <label>媒体类型</label>
              <select name="broadcast_media_type">
                <option value="">无</option>
                <option value="photo">图片</option>
                <option value="video">视频</option>
              </select>
              <div class="hint">选择后需在右侧填写对应的媒体 URL</div>
            </div>
            <div class="field">
              <label>媒体 URL</label>
              <input type="text" name="broadcast_media_url" placeholder="https://...">
              <div class="hint">图片或视频的直链地址</div>
            </div>
          </div>
          <div class="field">
            <label>内联按钮（可选）</label>
            <div class="hint" style="margin-bottom:8px">每行一个按钮，点击后跳转链接</div>
            <div id="broadcast-btn-rows"></div>
            <button type="button" class="btn-add" id="broadcast-btn-add">＋ 添加按钮</button>
            <input type="hidden" name="broadcast_buttons_json" id="broadcast_buttons_json">
          </div>
          <div style="margin-top:16px">
            <button type="submit" class="btn btn-primary">📢 发送广播</button>
          </div>
        </form>
      </div>
    </div>

  </div><!-- .content -->
</div><!-- .main -->
</div><!-- .layout -->
<script>{js}</script>
</body>
</html>
"""
