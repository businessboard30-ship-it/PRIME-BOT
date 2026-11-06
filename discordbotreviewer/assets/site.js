(() => {
  const API = "https://api-service-production-df63.up.railway.app/api/public_bot_directory";
  const grid = document.getElementById("bot-grid");
  const search = document.getElementById("search");
  const sort = document.getElementById("sort");
  const count = document.getElementById("result-count");
  const loadMore = document.getElementById("load-more");
  const categories = document.getElementById("categories");
  let category = "", query = "", offset = 0, total = 0, busy = false;
  const limit = 12;
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const fallback = name => (name || "B").trim().slice(0,1).toUpperCase();
  function card(bot) {
    const avatar = bot.avatar_url ? '<img class="bot-avatar" loading="lazy" src="'+esc(bot.avatar_url)+'" alt="">' : '<div class="bot-avatar bot-avatar-fallback">'+esc(fallback(bot.name))+'</div>';
    const tags = (bot.tags || []).slice(0,3).map(t => '<span class="tag">'+esc(t)+'</span>').join("");
    const invite = bot.invite_url ? '<a href="'+esc(bot.invite_url)+'" target="_blank" rel="noopener noreferrer">Add bot ↗</a>' : "";
    const share = '<a href="#" data-share="'+esc(bot.id)+'" data-name="'+esc(bot.name)+'">Share ↗</a>';
    return '<article class="bot-card"><div class="bot-head">'+avatar+'<div style="min-width:0"><div class="bot-name">'+esc(bot.name)+'</div><div class="bot-id">ID · '+esc(bot.application_id)+'</div></div></div><p class="bot-desc">'+esc(bot.description || "No description provided.")+'</p><div class="bot-meta"><span class="tag">'+esc(bot.category || "Other")+'</span>'+tags+'</div><div class="bot-foot"><span class="bot-stats">♡ '+Number(bot.votes || 0)+' · '+esc(bot.age_label || "Listed")+'</span><div class="bot-links">'+share+invite+'</div></div></article>';
  }
  async function load(reset=true) {
    if (busy) return;
    busy = true;
    if (reset) { offset = 0; grid.innerHTML = '<div class="empty-state"><span class="loader"></span><p>Finding approved bots…</p></div>'; }
    const params = new URLSearchParams({limit:String(limit),offset:String(offset),sort:sort.value});
    if (category) params.set("category",category);
    if (query) params.set("q",query);
    try {
      const response = await fetch(API+"?"+params.toString(),{headers:{Accept:"application/json"}});
      if (!response.ok) throw new Error("Directory API returned "+response.status);
      const data = await response.json();
      if (reset) grid.innerHTML = "";
      total = Number(data.total || 0);
      count.textContent = total+" approved "+(total === 1 ? "bot" : "bots");
      if (!data.items?.length && reset) grid.innerHTML = '<div class="empty-state">No bots match that search yet. Try another keyword or category.</div>';
      grid.insertAdjacentHTML("beforeend",(data.items || []).map(card).join(""));
      offset += (data.items || []).length;
      loadMore.hidden = offset >= total || total === 0;
    } catch (error) {
      if (reset) grid.innerHTML = '<div class="empty-state">The live directory could not connect right now.<br><small>PRIME-BOT API may be restarting. Please try again shortly.</small></div>';
      count.textContent = "Connection unavailable";
      loadMore.hidden = true;
    } finally { busy = false; }
  }
  let timer;
  search.addEventListener("input",() => { clearTimeout(timer); timer=setTimeout(()=>{query=search.value.trim();load(true)},250); });
  sort.addEventListener("change",()=>load(true));
  categories.addEventListener("click",event=>{
    const button=event.target.closest("[data-category]");
    if(!button)return;
    category=button.dataset.category;
    categories.querySelectorAll(".category").forEach(el=>el.classList.toggle("active",el===button));
    load(true);
  });
  loadMore.addEventListener("click",()=>load(false));
  grid.addEventListener("click",async event=>{
    const link=event.target.closest("[data-share]");
    if(!link)return;
    event.preventDefault();
    const url=location.origin+"/?bot="+encodeURIComponent(link.dataset.share);
    try {
      if(navigator.share) await navigator.share({title:link.dataset.name+" · Discord Bot Reviewer",url});
      else { await navigator.clipboard.writeText(url); link.textContent="Copied ✓"; }
    } catch (_) {}
  });
  const id = new URLSearchParams(location.search).get("bot");
  if(id) search.value=id, query=id;
  load(true);
})();