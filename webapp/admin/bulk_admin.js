"use strict";

/* YOUR SHOP Admin Collections
 * Premium 2/3-column management grids with bulk selection,
 * richer previews, image management and lightweight editors.
 */

(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const media = (v, fallback = "📦") => {
    const raw = String(v ?? "").trim();
    if (!raw) return `<div class="collection-thumb fallback">${fallback}</div>`;
    const src = raw.startsWith("tg:") ? `/media/telegram/${encodeURIComponent(raw.slice(3))}`
      : (/^https?:\/\//i.test(raw) || raw.startsWith("/") || raw.startsWith("data:")) ? raw
      : (/^[A-Za-z0-9_-]{20,}$/.test(raw) ? `/media/telegram/${encodeURIComponent(raw)}` : raw);
    return `<div class="collection-thumb"><img src="${esc(src)}" alt="" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='grid'"><span style="display:none">${fallback}</span></div>`;
  };

  async function request(url, options = {}) {
    const r = await fetch(url, {
      credentials: "include", cache: "no-store",
      ...options,
      headers: { Accept: "application/json", ...(options.body ? {"Content-Type":"application/json"} : {}), ...(options.headers || {}) }
    });
    const text = await r.text();
    let d = {};
    try { d = text ? JSON.parse(text) : {}; } catch { d = { error: text || "Invalid response" }; }
    if (!r.ok || d.ok === false) throw new Error(d.error || d.message || `Request failed (${r.status})`);
    return d;
  }

  function ensureStyle() {
    if (document.getElementById("bulkAdminInlineStyle")) return;
    const s = document.createElement("style");
    s.id = "bulkAdminInlineStyle";
    s.textContent = `
      .collection-topbar{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
      .collection-select-all{display:flex;align-items:center;gap:8px;font-weight:700;color:#dfe8f7}
      .collection-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
      .collection-card{position:relative;border:1px solid rgba(255,255,255,.08);border-radius:20px;background:linear-gradient(180deg,rgba(25,36,58,.98),rgba(15,23,39,.98));padding:14px;box-shadow:0 14px 38px rgba(0,0,0,.16);min-width:0}
      .collection-card:hover{transform:translateY(-2px);border-color:rgba(120,180,255,.25)}
      .collection-check{position:absolute;top:12px;left:12px;z-index:2;width:20px;height:20px}
      .collection-thumb{height:120px;border-radius:15px;overflow:hidden;background:#111a2b;display:grid;place-items:center;margin-bottom:12px;color:#7f8da7;font-size:38px}
      .collection-thumb img{width:100%;height:100%;object-fit:cover;display:block}
      .collection-thumb span{display:none;place-items:center;width:100%;height:100%}
      .collection-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
      .collection-name{font-weight:800;font-size:16px;line-height:1.25;word-break:break-word}
      .collection-id{color:#7f8da7;font-size:12px}
      .collection-meta{color:#aab7cc;font-size:12px;display:flex;flex-wrap:wrap;gap:7px;margin:9px 0}
      .collection-pill{padding:5px 8px;border-radius:999px;background:rgba(255,255,255,.06)}
      .collection-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-top:10px}
      .collection-actions .btn{width:100%}
      .bulk-toolbar{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:0 0 12px;padding:10px 12px;border-radius:14px;background:rgba(24,35,56,.72);border:1px solid rgba(255,255,255,.07);flex-wrap:wrap}
      .bulk-toolbar strong{color:#eaf2ff}
      .bulk-toolbar button:disabled{opacity:.45;cursor:not-allowed}
      .collection-card input[type=checkbox]{accent-color:#4ea1ff}
      .collection-modal{position:fixed;inset:0;z-index:5000;background:rgba(3,7,16,.78);display:none;align-items:center;justify-content:center;padding:16px}
      .collection-modal.active{display:flex}.collection-modal-card{width:min(680px,100%);max-height:90vh;overflow:auto;border-radius:24px;padding:20px;background:#10192b;color:#ecf4ff;border:1px solid rgba(255,255,255,.09);box-shadow:0 30px 90px rgba(0,0,0,.45)}
      .collection-modal-card h3{margin:0}.collection-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.collection-form-grid .full{grid-column:1/-1}.collection-modal label{display:grid;gap:6px;color:#aebbd0;font-size:13px}.collection-modal input,.collection-modal textarea,.collection-modal select{width:100%;box-sizing:border-box;border-radius:12px;border:1px solid rgba(255,255,255,.10);background:#18243a;color:#fff;padding:11px}.collection-modal textarea{min-height:110px;resize:vertical}.collection-modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}.photo-manager-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}.photo-manager-item{position:relative;aspect-ratio:1;border-radius:12px;overflow:hidden;background:#18243a}.photo-manager-item img{width:100%;height:100%;object-fit:cover}.photo-manager-item button{position:absolute;right:5px;top:5px}.photo-add-row{display:flex;gap:8px;margin-top:10px}.photo-add-row input{flex:1}
      @media(max-width:1000px){.collection-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
      @media(max-width:640px){.collection-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.collection-card{padding:10px;border-radius:15px}.collection-thumb{height:92px}.collection-name{font-size:13px}.collection-meta{gap:5px}.collection-actions{grid-template-columns:1fr;gap:5px}.collection-form-grid{grid-template-columns:1fr}.collection-form-grid .full{grid-column:auto}.photo-manager-grid{grid-template-columns:repeat(3,1fr)}}
    `;
    document.head.appendChild(s);
  }

  function statusPill(active) { return `<span class="collection-pill">${active ? "🟢 Active" : "🔴 Inactive"}</span>`; }

  function bulkBar(kind, count, total) {
    return `<div class="bulk-toolbar" data-bulk-kind="${kind}">
      <label class="collection-select-all"><input type="checkbox" data-bulk-select-all="${kind}"> Select all <span>${count ? `(${count}/${total})` : `(${total})`}</span></label>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <strong data-bulk-count="${kind}">${count} selected</strong>
        <button type="button" class="btn tiny danger" data-bulk-delete="${kind}" ${count ? "" : "disabled"}>Delete selected</button>
      </div>
    </div>`;
  }

  function renderCollection(boxId, kind, items, cardFactory) {
    const box = document.getElementById(boxId); if (!box) return;
    const selected = new Set(Array.from(document.querySelectorAll(`[data-bulk-item="${kind}"]:checked`)).map(x=>String(x.value)));
    const valid = new Set(items.map(x=>String(x.id)));
    for (const x of Array.from(selected)) if (!valid.has(x)) selected.delete(x);
    box.innerHTML = `<div class="bulk-admin-collection" data-kind-wrap="${kind}">${bulkBar(kind, selected.size, items.length)}<div class="collection-grid">${items.map(x=>cardFactory(x, selected.has(String(x.id)))).join("")}</div></div>`;
    box.querySelectorAll(`[data-bulk-item="${kind}"]`).forEach(cb => cb.addEventListener("change", () => refreshBulk(kind)));
    box.querySelector(`[data-bulk-select-all="${kind}"]`)?.addEventListener("change", e => {
      box.querySelectorAll(`[data-bulk-item="${kind}"]`).forEach(cb => cb.checked = e.target.checked);
      refreshBulk(kind);
    });
    box.querySelector(`[data-bulk-delete="${kind}"]`)?.addEventListener("click", () => bulkDelete(kind));
  }

  function refreshBulk(kind) {
    const checks = Array.from(document.querySelectorAll(`[data-bulk-item="${kind}"]`));
    const selected = checks.filter(x => x.checked).map(x=>x.value);
    document.querySelector(`[data-bulk-count="${kind}"]`)?.replaceChildren(document.createTextNode(`${selected.length} selected`));
    const del = document.querySelector(`[data-bulk-delete="${kind}"]`); if (del) del.disabled = selected.length === 0;
  }

  async function bulkDelete(kind) {
    const endpoint = {games:"/api/admin/games/bulk-delete",categories:"/api/admin/categories/bulk-delete",products:"/api/admin/products/bulk-delete",inventory:"/api/admin/inventory/bulk-delete",promos:"/api/admin/promo-codes/bulk-delete"}[kind];
    const ids = Array.from(document.querySelectorAll(`[data-bulk-item="${kind}"]:checked`)).map(x => Number(x.value));
    if (!endpoint || !ids.length) return;
    const label = kind.slice(0,-1);
    if (!window.confirm(`Delete ${ids.length} selected ${label}${ids.length===1?"":"s"}? This cannot be undone.`)) return;
    try {
      await request(endpoint,{method:"POST",body:JSON.stringify({ids})});
      if (kind==="games") await window.loadGames();
      else if (kind==="categories") await window.loadCategories();
      else if (kind==="products") await window.loadProducts();
      else if (kind==="inventory") await window.loadInventory();
      if (kind==="promos") window.location.reload();
    } catch(e){ alert(e.message); }
  }

  function addModal() {
    if (document.getElementById("collectionEditorModal")) return document.getElementById("collectionEditorModal");
    const m = document.createElement("div");
    m.id = "collectionEditorModal"; m.className = "collection-modal";
    m.innerHTML = `<div class="collection-modal-card"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><h3 id="collectionEditorTitle">Edit</h3><button class="btn tiny" type="button" data-editor-close>✕</button></div><form id="collectionEditorForm"><div id="collectionEditorFields" style="margin-top:14px"></div><div class="collection-modal-actions"><button class="btn" type="button" data-editor-close>Cancel</button><button class="btn primary" type="submit">Save changes</button></div></form></div>`;
    document.body.appendChild(m); m.addEventListener("click",e=>{if(e.target===m||e.target.closest("[data-editor-close]"))m.classList.remove("active")});
    return m;
  }

  async function editEntity(kind, id) {
    const source = {games: window.currentGames, categories: window.currentCategories, products: window.currentProducts}[kind] || [];
    let item = source.find(x=>String(x.id)===String(id));
    if (kind==="products") { try { item = (await request(`/api/admin/products/${id}`)).product; } catch(e){} }
    if (!item) return alert("Item not found");
    const m = addModal();
    $("collectionEditorTitle").textContent = `Edit ${kind.slice(0,-1)} #${id}`;
    const fields = kind==="products" ? `
      <div class="collection-form-grid"><label>Name<input name="name" required value="${esc(item.name)}"></label><label>Price (Stars)<input name="price_stars" type="number" min="1" value="${Number(item.price_stars||0)}"></label><label>Stock<input name="stock" type="number" min="0" value="${Number(item.stock||0)}"></label><label>Discount %<input name="discount_percent" type="number" min="0" max="100" value="${Number(item.discount_percent||0)}"></label><label class="full">Description<textarea name="description">${esc(item.description||"")}</textarea></label><label class="full">Banner / Image URL<input name="banner" value="${esc(item.banner||"")}" placeholder="Telegram file_id, tg:file_id, or https://..."></label><label><input name="featured" type="checkbox" ${item.featured?"checked":""}> Featured</label><label><input name="active" type="checkbox" ${item.active!==false?"checked":""}> Active</label></div>`
    : `<div class="collection-form-grid"><label class="full">Name<input name="name" required value="${esc(item.name)}"></label><label class="full">Description<textarea name="description">${esc(item.description||"")}</textarea></label><label class="full">Image / Telegram file_id<input name="image" value="${esc(item.image||"")}" placeholder="Paste Telegram file_id or image URL"></label><label>Sort order<input name="sort_order" type="number" value="${Number(item.sort_order||0)}"></label><label><input name="active" type="checkbox" ${item.active!==false?"checked":""}> Active</label></div>`;
    $("collectionEditorFields").innerHTML = fields;
    m.classList.add("active");
    $("collectionEditorForm").onsubmit = async (ev) => {
      ev.preventDefault(); const fd = new FormData(ev.currentTarget); const body = Object.fromEntries(fd.entries());
      body.active = !!fd.get("active"); body.featured = !!fd.get("featured");
      if (kind==="products") { body.price_stars=Number(body.price_stars||0); body.stock=Number(body.stock||0); body.discount_percent=Number(body.discount_percent||0); }
      else body.sort_order=Number(body.sort_order||0);
      try { await request(`/api/admin/${kind}/${id}`,{method:"PUT",body:JSON.stringify(body)}); m.classList.remove("active"); if(kind==="games")await window.loadGames();else if(kind==="categories")await window.loadCategories();else await window.loadProducts(); } catch(e){ alert(e.message); }
    };
  }

  async function managePhotos(productId) {
    const m=addModal(); $("collectionEditorTitle").textContent=`Manage Photos • Product #${productId}`; const f=$("collectionEditorFields");
    f.innerHTML=`<div id="photoManagerBody">Loading…</div>`; m.classList.add("active");
    async function render(){ const d=await request(`/api/admin/products/${productId}/images`); const images=d.images||[]; f.innerHTML=`<div class="muted">${images.length}/100 images. The first image is used as the product banner.</div><div class="photo-manager-grid">${images.map(img=>{const src=String(img.image||"").startsWith("tg:")?`/media/telegram/${encodeURIComponent(String(img.image).slice(3))}`:/^https?:\/\//i.test(img.image)?img.image:(/^[A-Za-z0-9_-]{20,}$/.test(String(img.image))?`/media/telegram/${encodeURIComponent(img.image)}`:img.image);return `<div class="photo-manager-item"><img src="${esc(src)}" onerror="this.style.opacity=.2"><button type="button" class="btn tiny danger" data-del-photo="${img.id}">✕</button></div>`}).join("")}</div><div class="photo-add-row"><input id="newProductPhoto" placeholder="Telegram file_id / tg:file_id / https://…"><button type="button" class="btn primary" id="addProductPhoto" ${images.length>=100?"disabled":""}>Add</button></div>`;
      f.querySelectorAll("[data-del-photo]").forEach(b=>b.addEventListener("click",async()=>{if(!confirm("Delete this image?"))return;try{await request(`/api/admin/products/${productId}/images/${b.dataset.delPhoto}`,{method:"DELETE"});await render();}catch(e){alert(e.message)}}));
      f.querySelector("#addProductPhoto")?.addEventListener("click",async()=>{const v=String(f.querySelector("#newProductPhoto")?.value||"").trim();if(!v)return;try{await request(`/api/admin/products/${productId}/images`,{method:"POST",body:JSON.stringify({image:v})});await render();}catch(e){alert(e.message)}});
    }
    await render();
  }

  function gameCard(g, checked){return `<article class="collection-card"><input class="collection-check" data-bulk-item="games" type="checkbox" value="${esc(g.id)}" ${checked?"checked":""}><div class="collection-thumb-wrap">${media(g.image,"🎮")}</div><div class="collection-head"><div><div class="collection-name">${esc(g.name||"Untitled Game")}</div><div class="collection-id">#${esc(g.id)}</div></div>${statusPill(g.active!==false)}</div><div class="collection-meta"><span class="collection-pill">${esc(g.slug||"No slug")}</span><span class="collection-pill">Order ${Number(g.sort_order||0)}</span></div><div class="collection-actions"><button class="btn tiny" type="button" data-edit-game="${g.id}">Edit</button><button class="btn tiny danger" type="button" data-delete-game="${g.id}">Delete</button></div></article>`}
  function catCard(c, checked){return `<article class="collection-card"><input class="collection-check" data-bulk-item="categories" type="checkbox" value="${esc(c.id)}" ${checked?"checked":""}><div>${media(c.image,"📂")}</div><div class="collection-head"><div><div class="collection-name">${esc(c.name||"Untitled Category")}</div><div class="collection-id">#${esc(c.id)} · ${esc(c.game_name||"Game")}</div></div>${statusPill(c.active!==false)}</div><div class="collection-meta"><span class="collection-pill">Order ${Number(c.sort_order||0)}</span></div><div class="collection-actions"><button class="btn tiny" type="button" data-edit-category="${c.id}">Edit</button><button class="btn tiny danger" type="button" data-delete-category="${c.id}">Delete</button></div></article>`}
  function prodCard(p, checked){const img=p.banner||p.first_image||"";return `<article class="collection-card"><input class="collection-check" data-bulk-item="products" type="checkbox" value="${esc(p.id)}" ${checked?"checked":""}><div>${media(img,"🛍️")}</div><div class="collection-head"><div><div class="collection-name">${esc(p.name||"Untitled Product")}</div><div class="collection-id">#${esc(p.id)}</div></div>${statusPill(p.active!==false)}</div><div class="collection-meta"><span class="collection-pill">⭐ ${Number(p.price_stars||0)}</span><span class="collection-pill">📦 ${Number(p.stock||0)}</span><span class="collection-pill">🖼️ ${Number(p.image_count||0)}</span></div><div class="collection-actions"><button class="btn tiny" type="button" data-edit-product="${p.id}">Edit</button><button class="btn tiny" type="button" data-photos-product="${p.id}">Photos</button><button class="btn tiny" type="button" data-view-product="${p.id}">Details</button><button class="btn tiny danger" type="button" data-delete-product="${p.id}">Delete</button></div></article>`}
  function invCard(i, checked){const sold=String(i.status||"").toLowerCase()==="sold";return `<article class="collection-card"><input class="collection-check" data-bulk-item="inventory" type="checkbox" value="${esc(i.id)}" ${checked?"checked":""}><div class="collection-head"><div><div class="collection-name">📦 ${esc(i.product_name||"Inventory Item")}</div><div class="collection-id">#${esc(i.id)}</div></div>${statusPill(!sold)}</div><div class="collection-meta"><span class="collection-pill">${sold?"🔴 Sold":"🟢 Available"}</span><span class="collection-pill">${esc(i.created_at||"")}</span></div><div class="collection-preview" style="background:rgba(255,255,255,.04);border-radius:12px;padding:10px;margin-bottom:9px;max-height:110px;overflow:auto"><code>${esc(i.item_data||"")}</code></div><div class="collection-actions"><button class="btn tiny" type="button" data-inv-status="${i.id}" data-status="${sold?"available":"sold"}">${sold?"Mark Available":"Mark Sold"}</button><button class="btn tiny danger" type="button" data-inv-delete="${i.id}" ${sold?"disabled":""}>Delete</button></div></article>`}

  function bindCollections(){
    document.addEventListener("click", async e=>{
      const b=e.target.closest("[data-edit-game]"); if(b)return editEntity("games",b.dataset.editGame);
      const c=e.target.closest("[data-edit-category]"); if(c)return editEntity("categories",c.dataset.editCategory);
      const p=e.target.closest("[data-edit-product]"); if(p)return editEntity("products",p.dataset.editProduct);
      const ph=e.target.closest("[data-photos-product]"); if(ph)return managePhotos(ph.dataset.photosProduct);
      const vd=e.target.closest("[data-view-product]"); if(vd)return window.viewProductDetails?.(Number(vd.dataset.viewProduct));
      const dg=e.target.closest("[data-delete-game]"); if(dg){if(confirm("Delete this game?"))try{await request(`/api/admin/games/${dg.dataset.deleteGame}`,{method:"DELETE"});await window.loadGames();}catch(x){alert(x.message)}}
      const dc=e.target.closest("[data-delete-category]"); if(dc){if(confirm("Delete this category?"))try{await request(`/api/admin/categories/${dc.dataset.deleteCategory}`,{method:"DELETE"});await window.loadCategories();}catch(x){alert(x.message)}}
      const dp=e.target.closest("[data-delete-product]"); if(dp){if(confirm("Delete this product?"))try{await request(`/api/admin/products/${dp.dataset.deleteProduct}`,{method:"DELETE"});await window.loadProducts();}catch(x){alert(x.message)}}
      const is=e.target.closest("[data-inv-status]"); if(is){try{await request(`/api/admin/inventory/${is.dataset.invStatus}/status`,{method:"POST",body:JSON.stringify({status:is.dataset.status})});await window.loadInventory();}catch(x){alert(x.message)}}
      const id=e.target.closest("[data-inv-delete]"); if(id && !id.disabled){if(confirm("Delete this inventory item?"))try{await request(`/api/admin/inventory/${id.dataset.invDelete}`,{method:"DELETE"});await window.loadInventory();}catch(x){alert(x.message)}}
    });
  }

  function init(){ensureStyle();bindCollections();
    let games=[], categories=[], products=[], inventory=[];
    function fillSelect(id, rows, placeholder){ const el=document.getElementById(id); if(!el)return; const old=el.value; el.innerHTML=`<option value="">${placeholder}</option>`+rows.map(x=>`<option value="${esc(x.id)}">${esc(x.name||('Item #'+x.id))}</option>`).join(""); if(old)el.value=old; }
    window.__premiumAdmin_renderGames=(items=games)=>renderCollection("gamesTable","games",items,gameCard);
    window.__premiumAdmin_renderCategories=(items=categories)=>renderCollection("categoriesTable","categories",items,catCard);
    window.__premiumAdmin_renderProducts=(items=products)=>{
      const term=(document.getElementById("productSearch")?.value||"").toLowerCase().trim();
      const gf=document.getElementById("productGameFilter")?.value||""; const cf=document.getElementById("productCategoryFilter")?.value||""; const sf=document.getElementById("productStatusFilter")?.value||"";
      const list=items.filter(x=>(!term||`${x.name||""} ${x.description||""}`.toLowerCase().includes(term))&&(!gf||String(x.game_id)===String(gf))&&(!cf||String(x.category_id)===String(cf))&&(!sf||(sf==="active"?x.active!==false:x.active===false)));
      renderCollection("productsTable","products",list,prodCard);
    };
    window.renderInventory=(items=inventory)=>{renderCollection("inventoryTable","inventory",items,invCard);window.renderInventoryCounts?.({total:items.length,available:items.filter(x=>String(x.status||"")!=="sold").length,sold:items.filter(x=>String(x.status||"")==="sold").length});};
    window.renderProducts=(items=products)=>window.__premiumAdmin_renderProducts(items);
    window.renderGames=(items=games)=>window.__premiumAdmin_renderGames(items);
    window.renderCategories=(items=categories)=>window.__premiumAdmin_renderCategories(items);
    window.__premiumAdmin_renderInventory=(items=inventory)=>{renderCollection("inventoryTable","inventory",items,invCard);window.renderInventoryCounts?.({total:items.length,available:items.filter(x=>String(x.status||"")!=="sold").length,sold:items.filter(x=>String(x.status||"")==="sold").length});};
    window.loadGames=async()=>{try{const d=await request("/api/admin/games");games=d.games||[];window.currentGames=games;window.__premiumAdmin_renderGames(games);fillSelect("categoryGame",games,"Select game");fillSelect("productGame",games,"Select game");fillSelect("inventoryProduct",products,"Select product");}catch(e){document.getElementById("gamesTable").innerHTML=`<div class="empty error">${esc(e.message)}</div>`;}};
    window.loadCategories=async()=>{try{const d=await request("/api/admin/categories");categories=d.categories||[];window.currentCategories=categories;const filter=document.getElementById("categoryGameFilter")?.value;if(filter)categories=categories.filter(x=>String(x.game_id)===String(filter));window.__premiumAdmin_renderCategories(categories);fillSelect("productCategory",categories,"Select category");fillSelect("productCategoryFilter",categories,"All categories");}catch(e){document.getElementById("categoriesTable").innerHTML=`<div class="empty error">${esc(e.message)}</div>`;}};
    window.loadProducts=async()=>{try{const d=await request("/api/admin/products");products=d.products||[];window.currentProducts=products;fillSelect("inventoryProduct",products,"Select product");fillSelect("productGameFilter",games,"All games");fillSelect("productCategoryFilter",categories,"All categories");window.__premiumAdmin_renderProducts(products);window.renderLowStock?.();}catch(e){document.getElementById("productsTable").innerHTML=`<div class="empty error">${esc(e.message)}</div>`;}};
    window.loadInventory=async()=>{try{const d=await request("/api/admin/inventory");inventory=d.inventory||[];window.currentInventory=inventory;window.__premiumAdmin_renderInventory(inventory);}catch(e){document.getElementById("inventoryTable").innerHTML=`<div class="empty error">${esc(e.message)}</div>`;}};
  }


  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded",init,{once:true}); else init();
})();
