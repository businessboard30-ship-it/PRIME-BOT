(() => {
  const slug = document.body.dataset.reviewSlug || "alka";
  const API = "https://api-service-production-df63.up.railway.app/api/public_bot_reviews?bot_id=" + encodeURIComponent(slug);
  const list = document.getElementById("review-list");
  const summary = document.getElementById("review-summary");
  const fill = document.getElementById("rating-bar-fill");
  const barLabel = document.getElementById("rating-bar-label");
  const form = document.getElementById("review-form");
  const status = document.getElementById("review-status");
  if (!list || !form) return;
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const stars = value => "★".repeat(Number(value)||0) + "☆".repeat(5-(Number(value)||0));
  function render(data) {
    const average=Number(data.average||0), count=Number(data.rating_count||0);
    summary.textContent = count ? average.toFixed(1) + " / 5 from " + count + " ratings · " + Number(data.review_count||0) + " reviews" : "Be the first to share feedback.";
    if(fill) fill.style.width=(Math.max(0,Math.min(5,average))/5*100)+"%";
    if(barLabel) barLabel.textContent=count ? average.toFixed(1)+" out of 5 average rating" : "No ratings yet";
    list.innerHTML = data.reviews.length ? data.reviews.map(item => '<article class="reader-review"><div class="reader-review-head"><strong>'+esc(item.name)+'</strong><span class="reader-stars">'+(item.rating ? stars(item.rating) : "Comment")+'</span></div>'+(item.comment ? '<p>'+esc(item.comment)+'</p>' : '')+'<time>'+new Date(item.created_at).toLocaleDateString()+'</time></article>').join("") : '<p class="reviews-empty">No reader feedback yet. You can start the conversation.</p>';
  }
  async function load() {
    try { const response = await fetch(API); if (!response.ok) throw new Error(); render(await response.json()); }
    catch (_) { summary.textContent = "Community feedback is temporarily unavailable."; list.innerHTML = ""; }
  }
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const rating = form.querySelector('input[name="rating"]:checked');
    const payload = {bot_id:slug,display_name:form.elements.display_name.value,comment:form.elements.comment.value,rating:rating ? Number(rating.value) : null,website:form.elements.website.value};
    button.disabled = true; status.textContent = "Sending your feedback…";
    try {
      const response = await fetch(API,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || "Could not submit feedback.");
      render(data); form.reset(); status.textContent = "Thanks. Your feedback has been published.";
    } catch (error) { status.textContent = error.message || "Could not submit feedback. Please try again."; }
    finally { button.disabled = false; }
  });
  load();
})();