import {useEffect, useState} from 'react';
import {apiRequest} from '../api/client.js';

export function RrcUploads({onChange}) {
  const [items,setItems] = useState({}), [target,setTarget] = useState(''), [stamp,setStamp] = useState('');
  const [file,setFile] = useState(null), [busy,setBusy] = useState(false), [message,setMessage] = useState('');
  async function refresh() { setItems(await apiRequest('/recall/rrc-transcripts')); }
  useEffect(()=>{refresh().catch(e=>setMessage(e.message));},[]);
  async function change(remove) {
    setBusy(true); setMessage('');
    try {
      const id = target || crypto.randomUUID();
      await apiRequest('/recall/rrc-transcripts/'+id, remove ? {method:'DELETE'} :
        {method:'PUT', body:JSON.stringify({name:file.name,content:await file.text(),recorded_at:stamp})});
      await refresh(); await onChange(); setTarget(''); setMessage(remove?'已删除':'索引已更新');
    } catch(e) { setMessage(e.message); } finally { setBusy(false); }
  }
  return <form onSubmit={e=>{e.preventDefault();change(false);}}><fieldset disabled={busy}>
    <legend>管理逐字稿</legend><label>逐字稿<select value={target} onChange={e=>{setTarget(e.target.value);setStamp(items[e.target.value]?.recorded_at?.slice(0,16)||'');}}>
      <option value="">添加新逐字稿</option>{Object.entries(items).map(([id,item])=><option key={id} value={id}>{item.name}</option>)}</select></label>
    <label>录制开始时间<input type="datetime-local" value={stamp} required onChange={e=>setStamp(e.target.value)}/></label>
    <label>逐字稿文件<input type="file" accept=".md,.txt" required onChange={e=>setFile(e.target.files[0])}/></label>
    <button disabled={!file||!stamp}>{target?'替换逐字稿':'上传并建立索引'}</button>
    {target&&<button type="button" onClick={()=>{if(window.confirm('删除这份逐字稿及其检索索引？'))change(true);}}>删除逐字稿</button>}
  </fieldset><span role="status">{busy?'正在更新索引…':message}</span></form>;
}
