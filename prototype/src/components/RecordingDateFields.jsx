export function recordingStamp(day, time, zone) {
  if (!day) throw new Error('请填写实际录制日期');
  return time ? `${day}T${time.length === 5 ? `${time}:00` : time}${zone === 'UTC' ? 'Z' : '+08:00'}` : day;
}
export function RecordingDateFields({ day, time, zone, onDay, onTime, onZone }) {
  return <fieldset className="recording-date-fields"><legend>实际录制时间</legend><label>录制日期<input type="date" required value={day} onChange={(e) => onDay(e.target.value)} /></label><label>已知起始时间（可选）<input type="time" value={time} onChange={(e) => onTime(e.target.value)} /></label><label>时区<select value={zone} onChange={(e) => onZone(e.target.value)}><option value="Asia/Shanghai">中国标准时间</option><option value="UTC">UTC</option></select></label><p>不知道具体时刻时只填日期，不会补成零点。</p></fieldset>;
}
