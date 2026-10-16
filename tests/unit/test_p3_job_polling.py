from pathlib import Path
import shutil
import subprocess


def test_terminal_job_keeps_raw_status_after_failed_or_overlapping_refresh():
    source = (Path(__file__).resolve().parents[2] / "inktime/app/web/templates/job_detail.html").read_text(encoding="utf-8")
    functions = "\n".join(line for line in source.splitlines() if line.startswith(("async function loadJob()", "function stopJobPoll()", "function scheduleJobPoll(")))
    node = shutil.which("node")
    assert node, "Hosted job polling regression requires Node"
    harness = """
const assert=require('node:assert/strict');
let rawJobStatus='completed',jobLoadInFlight=false,jobPollTimer=null,timers=0;
const terminalJobStatuses=new Set(['completed','completed_with_errors','failed','cancelled','budget_exceeded']);
const jobStatus={textContent:'已完成'};
const document={hidden:false,querySelector:()=>({textContent:''})};
const window={setTimeout:()=>{timers++;return timers},inktimeDecodeJson:async response=>response.json()};
const clearTimeout=()=>{};
let fetch=async()=>{throw Error('fixture offline')};
const renderJob=data=>data.status;
""" + functions + """
(async()=>{
  assert.equal(await loadJob(),'completed');scheduleJobPoll(await loadJob());assert.equal(timers,0);
  jobLoadInFlight=true;assert.equal(await loadJob(),'completed');jobLoadInFlight=false;
  rawJobStatus='running';scheduleJobPoll(await loadJob());assert.equal(timers,1);
  fetch=async()=>({ok:true,json:async()=>({status:'completed'})});
  scheduleJobPoll(await loadJob());assert.equal(rawJobStatus,'completed');assert.equal(timers,1);
})().catch(error=>{console.error(error);process.exit(1)});
"""
    subprocess.run([node, "-e", harness], check=True, capture_output=True, timeout=10)  # noqa: S603 -- repository controller and deterministic fixtures only
