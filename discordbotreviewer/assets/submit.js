(()=>{
const form=document.getElementById("article-submit-form");if(!form)return;
const status=document.getElementById("submit-status");
const polish=document.getElementById("ai-polish-button");
const preview=document.getElementById("ai-polish-preview");
const titlePreview=document.getElementById("ai-preview-title");
const summaryPreview=document.getElementById("ai-preview-summary");
const bodyPreview=document.getElementById("ai-preview-body");
const endpoint="https://api-service-production-df63.up.railway.app/api/editorial";
let original=null, pending=null, accepted=false;
const field=name=>form.elements.namedItem(name);
function hidden(name,value){let el=form.querySelector('input[name="'+name+'"]');if(!el){el=document.createElement("input");el.type="hidden";el.name=name;form.appendChild(el)}el.value=value}
function busy(button,on,label){button.disabled=on;button.textContent=on?label:button.dataset.idle}
polish.dataset.idle=polish.textContent;
document.getElementById("ai-discard-button")?.addEventListener("click",()=>{preview.hidden=true;pending=null;status.textContent="AI changes discarded. Your draft is unchanged."});
document.getElementById("ai-accept-button")?.addEventListener("click",()=>{
if(!pending)return;
if(!original)original={title:field("title").value,summary:field("summary").value,article_body:field("article_body").value};
field("title").value=titlePreview.value;
field("summary").value=summaryPreview.value;
field("article_body").value=bodyPreview.value;
hidden("original_title",original.title);hidden("original_summary",original.summary);hidden("original_article_body",original.article_body);hidden("ai_edited","true");
accepted=true;preview.hidden=true;pending=null;status.textContent="Polished version applied. Check it, then submit when ready.";
});
polish.addEventListener("click",async()=>{
const draft={title:field("title").value.trim(),category:field("category").value,summary:field("summary").value.trim(),article_body:field("article_body").value.trim()};
if(!draft.title||!draft.category||!draft.summary||draft.article_body.length<100){status.textContent="Complete the title, category, summary and at least 100 characters of article text first.";return}
if(draft.article_body.length>14000){status.textContent="Please keep the draft under 14,000 characters for AI polishing.";return}
if(!original)original={title:draft.title,summary:draft.summary,article_body:draft.article_body};
busy(polish,true,"Polishing your draft…");status.textContent="AI is checking your writing. This may take a few seconds.";
try{
const r=await fetch(endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...draft,action:"rewrite"})});
const d=await r.json();if(!r.ok||d.status!=="ok")throw new Error(d.message||"AI polishing failed.");
pending=d;titlePreview.value=d.title;summaryPreview.value=d.summary;bodyPreview.value=d.article_body;preview.hidden=false;status.textContent="Review the suggested edits below. Nothing has been changed or submitted yet.";preview.scrollIntoView({behavior:"smooth",block:"start"});
}catch(err){status.textContent=err.message||"AI polishing is temporarily unavailable. You can still submit your draft."}
finally{busy(polish,false,polish.dataset.idle)}
});
form.addEventListener("submit",async e=>{
e.preventDefault();const btn=form.querySelector('button[type="submit"]');const v=Object.fromEntries(new FormData(form).entries());v.action="submit";
if(accepted&&original){v.original_title=original.title;v.original_summary=original.summary;v.original_article_body=original.article_body;v.ai_edited="true"}
btn.disabled=true;status.textContent="Sending your article…";
try{const r=await fetch(endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(v)});const d=await r.json();if(!r.ok)throw new Error(d.message||"Submission failed.");form.reset();preview.hidden=true;original=null;pending=null;accepted=false;status.textContent=(d.message||"Sent for review.")+(d.submission_id?" Submission ID: "+d.submission_id+". Keep this ID for your records.":"")}
catch(err){status.textContent=err.message||"Could not submit your article."}
finally{btn.disabled=false}
});
})();