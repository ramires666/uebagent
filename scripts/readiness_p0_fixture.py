"""Static reduced DOM fixtures for bounded P0 model diagnostics."""
from __future__ import annotations


TREE_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Readiness tree fixture</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#202124;font:16px Arial,sans-serif}
main{width:760px;margin:90px auto;background:#fff;border:1px solid #d9dee8;padding:30px;min-height:420px}
h1{font-size:23px;margin:0 0 28px}.branch{display:flex;align-items:center;min-height:64px;border-bottom:1px solid #eee}
.expander{width:8px;height:8px;flex:none;margin:0 18px 0 9px;padding:0;border:1px solid #7c8798;background:#f8f9fb;font-size:0}
.check{width:13px;height:13px;margin:0 14px 0 0}.children{margin-left:66px;display:none}.children.open{display:block}
#result{margin-top:28px;color:#285c39;min-height:24px}
</style></head><body><main><h1>Choose a workspace</h1>
<div class="branch"><button class="expander" aria-label="Expand Projects" aria-expanded="false">+</button><span>Projects</span></div>
<div id="children" class="children"><label><input id="desktop" class="check" type="checkbox"> Desktop</label></div>
<div id="result" aria-live="polite"></div></main><script>
const exp=document.querySelector('.expander'),kids=document.querySelector('#children'),box=document.querySelector('#desktop');
exp.onclick=()=>{const open=kids.classList.toggle('open');exp.textContent=open?'-':'+';exp.setAttribute('aria-expanded',String(open))};
box.onchange=()=>{document.querySelector('#result').textContent=box.checked?'Selected: Desktop':''};
</script></body></html>"""

FORM_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Readiness form fixture</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#202124;font:16px Arial,sans-serif}
main{width:900px;margin:75px auto;background:#fff;border:1px solid #d9dee8;padding:28px;min-height:540px}
h1{font-size:23px;margin:0 0 20px}table{border-collapse:collapse;width:100%;table-layout:fixed}th,td{border:1px solid #d8dde6;padding:12px;text-align:left}
th{background:#edf1f7}td input{width:100%;height:36px;border:1px solid #aeb7c5;padding:6px;font:15px Arial}
button{margin-top:20px;padding:10px 18px;font:15px Arial}#result{min-height:25px;margin-top:20px;color:#285c39}
</style></head><body><main><h1>Add a team member</h1><form id="entry"><table><thead><tr><th>First name</th><th>Last name</th><th>Email</th><th>Age</th><th>Salary</th><th>Department</th></tr></thead>
<tbody><tr><td><input id="first" aria-label="First name"></td><td><input id="last" aria-label="Last name"></td><td><input id="email" aria-label="Email"></td><td><input id="age" aria-label="Age"></td><td><input id="salary" aria-label="Salary"></td><td><input id="department" aria-label="Department"></td></tr></tbody></table>
<button type="submit">Save row</button></form><div id="result" aria-live="polite"></div></main><script>
window.__fixtureSubmitCount=0;document.querySelector('#entry').onsubmit=e=>{e.preventDefault();window.__fixtureSubmitCount++;const vals=['first','last','email','age','salary','department'].map(id=>document.getElementById(id).value);document.querySelector('#result').textContent=vals.join(' | ')};
</script></body></html>"""

OVERLAY_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Readiness overlay fixture</title><style>
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;font:16px Arial,sans-serif;background:#f4f6fa;color:#202124}
main{position:relative;width:100%;height:100%}#target{position:fixed;left:500px;top:300px;width:220px;height:64px;z-index:1}
#overlay{position:fixed;left:500px;top:300px;width:220px;height:64px;z-index:2;background:rgba(30,70,120,.18);border:2px solid #315f91}
</style></head><body><main>
<button id="target">Target action</button><div id="overlay" aria-label="Covering overlay"></div>
<p>Target effects: <span id="target-count">0</span></p><p>Overlay effects: <span id="overlay-count">0</span></p>
<script>
window.__targetEffectCount=0;window.__overlayEffectCount=0;
document.querySelector('#target').onclick=()=>{window.__targetEffectCount++;document.querySelector('#target-count').textContent=String(window.__targetEffectCount)};
document.querySelector('#overlay').onclick=()=>{window.__overlayEffectCount++;document.querySelector('#overlay-count').textContent=String(window.__overlayEffectCount)};
</script></main></body></html>"""


def fixture_html(scenario: str) -> str:
    """Return fixed content for an allowlisted synthetic scenario."""
    if scenario == "tree":
        return TREE_HTML
    if scenario == "form":
        return FORM_HTML
    if scenario == "overlay":
        return OVERLAY_HTML
    raise ValueError("unsupported P0 fixture scenario")
