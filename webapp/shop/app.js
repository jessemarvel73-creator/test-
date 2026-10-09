"use strict";

/* YOUR SHOP Mini App
 * Cleaned + hardened version of app.js.
 * Keeps the existing API contract and adds safer event delegation,
 * notifications support, refresh helpers, debounced search and better
 * loading/error handling.
 */

document.body?.setAttribute("data-app-js", "running");

let tg = null;

function initTelegramWebApp() {
  tg = window.Telegram?.WebApp || null;
  if (tg) {
    try {
      tg.ready();
      tg.expand();
    } catch (e) {
      console.warn("Telegram WebApp initialization warning:", e);
    }
  }
  return tg;
}

initTelegramWebApp();

const state = {
  games: [],
  categories: [],
  products: [],
  cart: [],
  orders: [],
  gameId: null,
  categoryId: null,
  search: "",
  selectedOrderId: null,
  referral: null,
  promo: null,
  favorites: [],
  favoriteIds: new Set(),
  selectedProductId: null,
  tickets: [],
  selectedTicketId: null,
  notifications: [],
  notificationsUnread: 0,
  content: {}
};

const $ = (id) => document.getElementById(id);
const initData = () => tg?.initData || "";

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function toast(message) {
  const x = $("toast");
  if (!x) return;
  x.textContent = String(message ?? "");
  x.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => x.classList.remove("show"), 2200);
}

function salePrice(item) {
  const base = Number(item?.price_stars || 0);
  const percent = Math.max(0, Math.min(100, Number(item?.discount_percent || 0)));
  if (base <= 0) return 0;
  return Math.max(1, base - Math.floor(base * percent / 100));
}

async function api(url, opts = {}) {
  const request = { ...opts };
  const headers = {
    Accept: "application/json",
    ...(request.headers || {})
  };

  if (request.body && typeof request.body === "string") {
    try {
      const body = JSON.parse(request.body);
      body.init_data = initData();
      request.body = JSON.stringify(body);
      headers["Content-Type"] = "application/json";
    } catch (_) {
      // Non-JSON bodies are left unchanged.
    }
  }

  if (initData()) headers["X-Telegram-Init-Data"] = initData();

  const response = await fetch(url, {
    cache: "no-store",
    ...request,
    headers
  });

  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `Request failed (${response.status})`);
  }
  return data;
}

function empty(text) {
  return `<div class="empty">${esc(text)}</div>`;
}

function show(view) {
  document.querySelectorAll(".view").forEach((x) => x.classList.remove("active"));
  const target = $(view);
  if (target) target.classList.add("active");

  document.querySelectorAll(".nav-item").forEach((x) => {
    x.classList.toggle(
      "active",
      x.dataset.nav === view ||
      (view === "productView" && x.dataset.nav === "homeView") ||
      (view === "orderView" && x.dataset.nav === "ordersView")
    );
  });
  window.scrollTo(0, 0);
}

async function loadPublicContent() {
  try {
    const d = await api("/api/content");
    state.content = d.content || {};
    applyPublicContent();
  } catch (e) {
    console.warn("Public content load failed:", e.message);
  }
}

function applyPublicContent() {
  const content = state.content || {};
  const setText = (selector, key) => {
    const el = document.querySelector(selector);
    const value = String(content[key] || "").trim();
    if (el && value) el.textContent = value;
  };

  setText("#homeView .hero p", "shop_description");
  setText("#homeView [data-content-card='giveaways']", "giveaways_description");
  setText("#homeView [data-content-card='promo']", "promo_purchase_description");
  setText("#homeView [data-content-card='referral']", "referral_description");
  setText("#homeView [data-content-card='feedback']", "feedback_description");
  setText("#homeView [data-content-card='notifications']", "notifications_description");
  setText("#homeView [data-content-card='support']", "support_description");

  setText("#ordersView #ordersDescription", "orders_description");
  setText("#notificationsView #notificationsDescription", "notifications_description");
  setText("#giveawaysView #giveawaysDescription", "giveaways_description");
  setText("#promoView #promoDescription", "promo_purchase_description");
  setText("#feedbackView #feedbackDescription", "feedback_description");
  setText("#supportView #supportDescription", "support_description");
}

async function loadGames() {
  const d = await api("/api/games");
  state.games = d.games || [];
  const box = $("games");
  if (!box) return;

  box.innerHTML = state.games.length
    ? state.games.map((g) => {
        const description = String(g.description || "").trim();
        return `
        <button class="chip game-chip ${state.gameId == g.id ? "active" : ""}" data-game="${esc(g.id)}" title="${esc(description)}">
          ${g.image ? imageMarkup(g.image,g.name) : "🎮"}
          <span><strong>${esc(g.name)}</strong><small>${esc(description || "Browse products")}</small></span>
        </button>
      `;
      }).join("")
    : empty("No games yet");
}

async function loadCategories() {
  const box = $("categories");
  if (!box) return;

  if (!state.gameId) {
    state.categories = [];
    box.innerHTML = `<div class="muted">Showing all categories.</div>`;
    return;
  }

  const d = await api(`/api/games/${encodeURIComponent(state.gameId)}/categories`);
  state.categories = d.categories || [];
  box.innerHTML = `
    <button class="chip ${state.categoryId === null ? "active" : ""}" data-category="all">All</button>
    ${state.categories.map((c) => {
      const description = String(c.description || "").trim();
      return `
      <button class="chip category-chip ${state.categoryId == c.id ? "active" : ""}" data-category="${esc(c.id)}" title="${esc(description)}">
        ${c.image ? imageMarkup(c.image,c.name) : "📂"}
        <span><strong>${esc(c.name)}</strong><small>${esc(description || "Browse products")}</small></span>
      </button>
    `;
    }).join("")}
  `;
}

async function loadProducts() {
  let url = "/api/products";
  const q = new URLSearchParams();
  if (state.gameId) q.set("game_id", state.gameId);
  if (state.categoryId) q.set("category_id", state.categoryId);
  if (q.toString()) url += `?${q.toString()}`;

  const d = await api(url);
  state.products = d.products || [];
  renderProducts();
}

function resolveImageSource(value) {
  const v = String(value || "").trim();
  if (!v) return "";
  if (/^(https?:|data:|blob:)/i.test(v)) return v;
  if (/^\/media\/telegram\//i.test(v)) return v;
  if (v.startsWith("tg:")) return `/media/telegram/${encodeURIComponent(v.slice(3))}`;
  // Telegram Bot API file_id or a bare stored media identifier.
  return `/media/telegram/${encodeURIComponent(v)}`;
}

function imageMarkup(value, alt = "", className = "") {
  const src = resolveImageSource(value);
  if (!src) return `<span class="img-fallback">🖼️</span>`;
  return `<img class="${esc(className)}" src="${esc(src)}" alt="${esc(alt)}" loading="lazy"
    onerror="this.dataset.failed='1';this.replaceWith(Object.assign(document.createElement('span'),{className:'img-fallback',textContent:'🖼️'}))">`;
}

function productImage(p, className = "") {
  const candidates = [
    p?.banner,
    p?.first_image,
    ...(Array.isArray(p?.images) ? p.images.map(x => typeof x === "string" ? x : x?.image) : [])
  ].filter(Boolean);
  return imageMarkup(candidates[0] || "", p?.name || "", className);
}


function renderFeatured() {
  const box = $("featuredProducts");
  if (!box) return;
  const featured = state.products.filter((p) => Boolean(p.featured));
  box.innerHTML = featured.length
    ? featured.slice(0, 8).map(featuredCard).join("")
    : `<div class="muted">Featured products will appear here.</div>`;
}

function featuredCard(p) {
  const stock = Number(p.stock || 0);
  const price = salePrice(p);
  const original = Number(p.price_stars || 0);
  const discount = Math.max(0, Math.min(100, Number(p.discount_percent || 0)));
  return `
    <button class="featured-card" data-product="${esc(p.id)}">
      <div class="featured-image">${productImage(p)}</div>
      <div class="featured-body">
        <span class="featured-pill">FEATURED</span>
        <strong>${esc(p.name)}</strong>
        <span class="featured-price">⭐ ${price}${discount ? ` <s>⭐ ${original}</s> -${discount}%` : ""}</span>
        <small>${stock > 0 ? `${stock} in stock` : "Sold out"}</small>
      </div>
    </button>
  `;
}

function renderProducts() {
  renderFeatured();
  const term = state.search.toLowerCase().trim();
  const list = state.products.filter((p) => [
    p.name, p.description, p.game_name, p.category_name
  ].join(" ").toLowerCase().includes(term));

  const resultCount = $("resultCount");
  if (resultCount) resultCount.textContent = `${list.length} item${list.length === 1 ? "" : "s"}`;

  const products = $("products");
  if (!products) return;
  products.innerHTML = list.length ? list.map(productCard).join("") : empty("No products found.");
}

function productCard(p) {
  const stock = Number(p.stock || 0);
  const price = salePrice(p);
  const original = Number(p.price_stars || 0);
  const discount = Math.max(0, Math.min(100, Number(p.discount_percent || 0)));
  const id = Number(p.id);
  const isFav = state.favoriteIds.has(id);

  return `
    <article class="product-card favorite-product-card">
      ${p.featured ? `<span class="featured-badge">FEATURED</span>` : ""}
      <button class="heart-btn ${isFav ? "active" : ""}" data-favorite="${esc(id)}" aria-label="${isFav ? "Remove from favorites" : "Add to favorites"}">
        ${isFav ? "♥" : "♡"}
      </button>
      <button style="all:unset;display:block;width:100%;cursor:pointer" data-product="${esc(id)}">
        <div class="product-image">${productImage(p)}</div>
        <div class="product-body">
          <h3>${esc(p.name)}</h3>
          <div class="product-meta">${esc(p.category_name || p.game_name || "")}</div>
          <div class="price-row">
            <span class="price">⭐ ${price}${discount ? ` <s>⭐ ${original}</s> -${discount}%` : ""}</span>
            <span class="stock-text ${stock > 0 ? "" : "sold-out"}">${stock > 0 ? `${stock} left` : "Sold out"}</span>
          </div>
        </div>
      </button>
      <div style="padding:0 11px 11px">
        <button class="add-btn" style="width:100%" data-add="${esc(id)}" ${stock <= 0 ? "disabled" : ""}>
          ${stock > 0 ? "Add to Cart" : "Out of Stock"}
        </button>
      </div>
    </article>
  `;
}

async function loadFavorites() {
  try {
    const d = await api("/api/favorites", {
      method: "POST",
      body: JSON.stringify({})
    });
    state.favorites = d.favorites || [];
    state.favoriteIds = new Set((d.favorite_ids || []).map(Number));
    renderFavorites();
    renderProducts();
  } catch (e) {
    console.error("Favorites load failed:", e);
  }
}

async function toggleFavorite(productId) {
  const id = Number(productId);
  const adding = !state.favoriteIds.has(id);
  try {
    const d = await api(
      adding ? "/api/favorites/add" : "/api/favorites/remove",
      {
        method: "POST",
        body: JSON.stringify({ product_id: id })
      }
    );
    state.favorites = d.favorites || [];
    state.favoriteIds = new Set((d.favorite_ids || []).map(Number));
    renderProducts();
    renderFavorites();
    if (state.selectedProductId === id) await renderCurrentProductDetail();
    toast(adding ? "Added to Favorites" : "Removed from Favorites");
  } catch (e) {
    toast(e.message);
  }
}

function renderFavorites() {
  const box = $("favoritesList");
  if (!box) return;

  if (!state.favorites.length) {
    box.innerHTML = empty("No favorites yet. Tap ♥ on a product to save it here.");
    return;
  }

  box.innerHTML = `
    <div class="favorites-note">Your saved products appear here for quick access.</div>
    <div class="product-grid">${state.favorites.map(productCard).join("")}</div>
  `;
}

async function loadCart() {
  const d = await api("/api/cart/get", {
    method: "POST",
    body: JSON.stringify({})
  });
  state.cart = d.items || [];
  updateBadge();
  renderCart();
}

function updateBadge() {
  const badge = $("cartBadge");
  if (!badge) return;
  const count = state.cart.reduce(
    (sum, x) => sum + Number(x.quantity || 0),
    0
  );
  badge.textContent = String(count);
  badge.hidden = count <= 0;
}

function cartTotal() {
  return state.cart.reduce(
    (sum, x) =>
      sum + salePrice(x) * Number(x.quantity || 0),
    0
  );
}

function renderCart() {
  const box = $("cartItems");
  const summary = $("cartSummary");
  if (!box || !summary) return;

  if (!state.cart.length) {
    box.innerHTML = empty("Your cart is empty.");
    summary.innerHTML = "";
    return;
  }

  box.innerHTML = state.cart.map((x) => `
    <div class="cart-row">
      <div class="cart-thumb">
        ${x.banner
          ? `<img src="${esc(x.banner)}" alt="${esc(x.name)}">`
          : "🛍️"}
      </div>

      <div class="cart-info">
        <h3>${esc(x.name)}</h3>
        <div class="muted">⭐ ${salePrice(x)} each${Number(x.discount_percent || 0) ? ` <s>⭐ ${Number(x.price_stars || 0)}</s> -${Number(x.discount_percent)}%` : ""}</div>

        <div class="cart-controls">
          <div class="qty">
            <button data-qty="-" data-id="${esc(x.product_id)}">−</button>
            <strong>${Number(x.quantity || 0)}</strong>
            <button data-qty="+" data-id="${esc(x.product_id)}">+</button>
          </div>

          <button class="remove" data-remove="${esc(x.product_id)}">
            Remove
          </button>
        </div>
      </div>

      <strong>
        ⭐ ${salePrice(x) * Number(x.quantity || 0)}
      </strong>
    </div>
  `).join("");

  const total = cartTotal();
  const count = state.cart.reduce(
    (sum, x) => sum + Number(x.quantity || 0),
    0
  );

  summary.innerHTML = `
    <div class="summary">
      <div class="sum-row">
        <span>Items</span>
        <span>${count}</span>
      </div>

      <div class="sum-row total">
        <span>Total</span>
        <span>⭐ ${total}</span>
      </div>

      <button class="primary-btn" id="checkoutBtn">
        Checkout with Telegram Stars
      </button>
    </div>
  `;

  $("checkoutBtn")?.addEventListener("click", openCheckout);
}

async function addCart(id) {
  try {
    const d = await api("/api/cart/add", {
      method: "POST",
      body: JSON.stringify({
        product_id: Number(id),
        quantity: 1
      })
    });

    state.cart = d.items || [];
    state.promo = null;

    updateBadge();
    renderCart();
    toast("Added to cart");
  } catch (e) {
    toast(e.message);
  }
}

async function changeQty(id, delta) {
  const item = state.cart.find(
    (x) => Number(x.product_id) === Number(id)
  );

  if (!item) return;

  const quantity = Number(item.quantity) + delta;

  if (quantity <= 0) {
    return removeCart(id);
  }

  try {
    const d = await api("/api/cart/update", {
      method: "POST",
      body: JSON.stringify({
        product_id: Number(id),
        quantity
      })
    });

    state.cart = d.items || [];
    state.promo = null;

    updateBadge();
    renderCart();
  } catch (e) {
    toast(e.message);
  }
}

async function removeCart(id) {
  try {
    const d = await api("/api/cart/remove", {
      method: "POST",
      body: JSON.stringify({
        product_id: Number(id)
      })
    });

    state.cart = d.items || [];
    state.promo = null;

    updateBadge();
    renderCart();
  } catch (e) {
    toast(e.message);
  }
}

async function openProduct(id) {
  state.selectedProductId = Number(id);
  show("productView");

  const details = $("productDetails");
  if (!details) return;

  details.innerHTML = empty("Loading...");

  try {
    const d = await api(
      `/api/products/${encodeURIComponent(Number(id))}`
    );
    renderDetail(d.product);
  } catch (e) {
    details.innerHTML = empty(e.message);
  }
}

function renderDetail(p) {
  if (!p) return;

  state.selectedProductId = Number(p.id);

  const imgs = Array.isArray(p.images) ? p.images : [];

  const rawGallery = [
    ...imgs.map(x => typeof x === "string" ? {image:x} : x),
    ...(p.banner && !imgs.some(x => (typeof x === "string" ? x : x?.image) === p.banner) ? [{image:p.banner}] : [])
  ].filter(x => x && x.image);
  const galleryImages = rawGallery.slice(0, 100);
  const gallery = galleryImages.length
    ? `
      <div class="product-gallery premium-gallery" data-gallery>
        <div class="gallery-main">
          <img id="galleryMainImage" src="${esc(resolveImageSource(galleryImages[0].image))}" alt="${esc(p.name)}">
          ${galleryImages.length > 1 ? `
            <button type="button" class="gallery-arrow left" data-gallery-prev aria-label="Previous image">‹</button>
            <button type="button" class="gallery-arrow right" data-gallery-next aria-label="Next image">›</button>
          ` : ""}
          <div class="gallery-counter"><span id="galleryIndex">1</span> / ${galleryImages.length}</div>
        </div>
        ${galleryImages.length > 1 ? `
          <div class="gallery-strip">
            ${galleryImages.map((i,idx) => `
              <button type="button" class="gallery-thumb ${idx===0?'active':''}" data-gallery-index="${idx}" aria-label="Image ${idx+1}">
                ${imageMarkup(i.image, p.name)}
              </button>`).join("")}
          </div>
          <div class="gallery-hint">Swipe or use the arrows to view all ${galleryImages.length} images</div>
        ` : ""}
      </div>
    `
    : `<div class="detail-placeholder">${productImage(p)}</div>`;

  const stock = Number(p.stock || 0);
  const favorite = state.favoriteIds.has(Number(p.id));

  $("productDetails").innerHTML = `
    <article class="detail-card">
      ${gallery}

      <div class="detail-body">
        <div class="detail-title-row">
          <h1>${esc(p.name)}</h1>

          <button
            class="detail-heart ${favorite ? "active" : ""}"
            data-favorite="${esc(p.id)}"
            aria-label="Favorite"
          >
            ${favorite ? "♥" : "♡"}
          </button>
        </div>

        <div class="muted">
          🎮 ${esc(p.game_name || "")}
          ${p.category_name
            ? ` • 📂 ${esc(p.category_name)}`
            : ""}
        </div>

        <p class="description">
          ${esc(p.description || "No description available.")}
        </p>

        <div class="detail-price">
          ⭐ ${salePrice(p)} Stars
          ${Number(p.discount_percent || 0) ? `<s>⭐ ${Number(p.price_stars || 0)}</s> <small>-${Number(p.discount_percent)}%</small>` : ""}
        </div>

        <div class="muted" style="margin-bottom:12px">
          ${stock > 0 ? `${stock} available` : "Out of stock"}
        </div>
        ${stock <= 0 ? `<button id="stockAlertBtn" class="secondary-btn">🔔 Notify me when available</button>` : ""}

        <button
          class="primary-btn"
          data-add="${esc(p.id)}"
          ${stock <= 0 ? "disabled" : ""}
        >
          ${stock > 0 ? "Add to Cart" : "Out of Stock"}
        </button>

        <div id="productReviews" class="reviews-panel"></div>

        <button
          class="secondary-btn"
          id="buyNow"
          ${stock <= 0 ? "disabled" : ""}
        >
          Buy Now
        </button>
      </div>
    </article>
  `;

  const galleryState = { index: 0, images: galleryImages };
  const setGallery = (idx) => {
    if (!galleryState.images.length) return;
    galleryState.index = (idx + galleryState.images.length) % galleryState.images.length;
    const img = $("galleryMainImage"); if (img) img.src = resolveImageSource(galleryState.images[galleryState.index].image);
    const counter=$("galleryIndex"); if(counter) counter.textContent=String(galleryState.index+1);
    document.querySelectorAll("[data-gallery-index]").forEach((x,i)=>x.classList.toggle("active",i===galleryState.index));
  };
  document.querySelector("[data-gallery-prev]")?.addEventListener("click",()=>setGallery(galleryState.index-1));
  document.querySelector("[data-gallery-next]")?.addEventListener("click",()=>setGallery(galleryState.index+1));
  document.querySelectorAll("[data-gallery-index]").forEach(x=>x.addEventListener("click",()=>setGallery(Number(x.dataset.galleryIndex))));
  const gm=document.querySelector("[data-gallery]"); let sx=0;
  gm?.addEventListener("touchstart",e=>{sx=e.changedTouches[0].clientX},{passive:true});
  gm?.addEventListener("touchend",e=>{const dx=e.changedTouches[0].clientX-sx;if(Math.abs(dx)>35)setGallery(galleryState.index+(dx<0?1:-1));},{passive:true});
  loadProductReviews(p.id);

  $("stockAlertBtn")?.addEventListener("click",async()=>{try{const d=await api(`/api/products/${p.id}/stock-alert`,{method:"POST",body:JSON.stringify({})});toast(d.alert?"Stock alert enabled":"Already enabled");}catch(e){toast(e.message);}});

  $("buyNow")?.addEventListener("click", async () => {
    await addCart(p.id);
    await loadCart();
    openCheckout();
  });
}

async function openCheckout() {
  if (!state.cart.length) {
    return toast("Your cart is empty");
  }

  if (!state.referral) {
    await loadReferral().catch(() => {});
  }
  show("checkoutView");
  renderCheckout();
  window.CPMRewards?.refreshCheckout?.();
}

function checkoutDiscountBreakdown() {
  const subtotal = cartTotal();
  const promoDiscount = Math.min(subtotal, Number(state.promo?.discount_stars || 0));
  const afterPromo = Math.max(0, subtotal - promoDiscount);
  const referralPercent = Math.max(0, Math.min(100, Number(state.referral?.active_discount_percent || 0)));
  const referralDiscount = Math.min(afterPromo, Math.floor(afterPromo * referralPercent / 100));
  const afterReferral = Math.max(0, afterPromo - referralDiscount);
  const loyaltyDiscount = Math.min(
    afterReferral,
    Number(window.CPMRewards?.getSelectedDiscount?.() || 0)
  );
  const total = Math.max(0, afterReferral - loyaltyDiscount);
  return { subtotal, promoDiscount, referralDiscount, referralPercent, loyaltyDiscount, total };
}

function discountedTotal() {
  return checkoutDiscountBreakdown().total;
}

window.CPMShopRenderCheckout = function () { renderCheckout(); };

function renderCheckout() {
  const breakdown = checkoutDiscountBreakdown();
  const subtotal = breakdown.subtotal;
  const discount = breakdown.promoDiscount;
  const referralDiscount = breakdown.referralDiscount;
  const loyaltyDiscount = breakdown.loyaltyDiscount;
  const total = breakdown.total;

  const promoLabel = state.promo?.code
    ? `
      <div class="promo-applied">
        <div>
          <strong>✅ ${esc(state.promo.code)}</strong>
          <span>Save ⭐ ${discount}</span>
        </div>
        <button id="removePromo" class="text-btn">
          Remove
        </button>
      </div>
    `
    : "";

  const loyaltyPanel = loyaltyDiscount > 0
    ? `
      <div class="checkout-rewards-panel">
        <div>
          <strong>⭐ Loyalty reward applied</strong>
          <span>Save ⭐ ${loyaltyDiscount}</span>
        </div>
        <button id="checkoutCancelReward" class="text-btn" type="button">Remove</button>
      </div>
    `
    : "";

  $("checkoutBox").innerHTML = `
    <div class="checkout-note">
      You will pay securely using Telegram Stars.
      Your promo code is checked again when the payment
      invoice is created.
    </div>

    <div class="promo-box">
      <div class="promo-title">🎟️ Promo Code</div>

      <div class="promo-input-row">
        <input
          id="promoInput"
          class="promo-input promo-code-input"
          value="${esc(state.promo?.code || "")}"
          placeholder="Enter promo code"
          maxlength="40"
          autocomplete="off"
        >

        <button
          id="applyPromo"
          class="secondary-btn"
        >
          Apply
        </button>
      </div>

      ${promoLabel}
      ${loyaltyPanel}

      <div id="promoMessage" class="promo-message">
        ${esc(state.promo?.message || "")}
      </div>
    </div>

    <div class="summary">
      <div class="sum-row">
        <span>Products</span>
        <span>
          ${state.cart.reduce(
            (a, x) => a + Number(x.quantity || 0),
            0
          )}
        </span>
      </div>

      <div class="sum-row">
        <span>Subtotal</span>
        <span>⭐ ${subtotal}</span>
      </div>

      ${
        discount > 0
          ? `
            <div class="sum-row discount">
              <span>Promo discount</span>
              <span>-⭐ ${discount}</span>
            </div>
          `
          : ""
      }

      ${
        referralDiscount > 0
          ? `
            <div class="sum-row discount">
              <span>Referral discount</span>
              <span>-⭐ ${referralDiscount}</span>
            </div>
          `
          : ""
      }

      ${
        loyaltyDiscount > 0
          ? `
            <div class="sum-row discount">
              <span>Loyalty reward</span>
              <span>-⭐ ${loyaltyDiscount}</span>
            </div>
          `
          : ""
      }

      <div class="sum-row total">
        <span>Total</span>
        <span>⭐ ${total} Stars</span>
      </div>

      <button id="payBtn" class="primary-btn">
        ${total === 0 ? "🎁 Complete Free Order" : `⭐ Pay ${total} Stars`}
      </button>
    </div>
  `;

  $("applyPromo")?.addEventListener("click", applyPromo);
  $("checkoutCancelReward")?.addEventListener("click", () => {
    window.CPMRewards?.cancel?.();
  });

  $("removePromo")?.addEventListener("click", () => {
    state.promo = null;
    renderCheckout();
  });

  $("promoInput")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      applyPromo();
    }
  });

  $("payBtn")?.addEventListener("click", startCheckout);
}

async function applyPromo() {
  const input = $("promoInput");
  const code = String(input?.value || "").trim().toUpperCase();
  const msg = $("promoMessage");

  if (!code) {
    state.promo = null;
    if (msg) {
      msg.textContent = "No promo code entered. You can pay normally with Stars.";
      msg.className = "promo-message";
    }
    return;
  }

  const button = $("applyPromo");
  if (button) {
    button.disabled = true;
    button.textContent = "Checking…";
  }

  try {
    const d = await api("/api/promo/validate", {
      method: "POST",
      body: JSON.stringify({ code, subtotal_stars: cartTotal() })
    });
    state.promo = d.promo || null;
    renderCheckout();
    toast(`Promo applied: ${code}`);
  } catch (e) {
    state.promo = null;
    if (msg) {
      msg.textContent = e.message || "Promo code could not be applied.";
      msg.className = "promo-message error";
    }
    if (button) {
      button.disabled = false;
      button.textContent = "Apply";
    }
  }
}

async function startCheckout() {
  if (!state.cart.length) {
    return toast("Your cart is empty");
  }

  const button = $("payBtn");
  if (button) {
    button.disabled = true;
    button.textContent = "Creating invoice...";
  }

  try {
    const enteredPromo = String($("promoInput")?.value || "").trim().toUpperCase();
    const loyaltyRedemptionId = window.CPMRewards?.getSelectedId?.() || null;
    const payload = {
      ...(state.promo?.code && enteredPromo ? {promo_code: state.promo.code} : {}),
      ...(loyaltyRedemptionId ? {loyalty_redemption_id: loyaltyRedemptionId} : {}),
      subtotal_stars: cartTotal()
    };

    const d = await api("/api/cart/checkout", {
      method: "POST",
      body: JSON.stringify(payload)
    });

    if (d.free_order && d.order_id) {
      state.cart = [];
      state.promo = null;
      updateBadge();
      await loadOrders();
      showSuccess(d.order_id);
      return;
    }

    if (d.invoice_url && tg?.openInvoice) {
      tg.openInvoice(d.invoice_url, async (status) => {
        if (status === "paid") {
          state.cart = [];
          state.promo = null;
          updateBadge();

          try {
            await loadCart();
          } catch (_) {
            // Keep success state even if cart refresh fails.
          }

          if (d.order_id) {
            await loadOrders().catch(() => {});
            showSuccess(d.order_id);
          } else {
            showSuccess(null);
          }
        } else if (status === "cancelled") {
          if (d.order_id) {
            await api(`/api/orders/${Number(d.order_id)}/cancel`, {
              method: "POST",
              body: JSON.stringify({})
            }).catch(() => {});
          }
          state.promo = null;
          toast("Payment cancelled");
          renderCheckout();
        } else if (status === "failed") {
          if (d.order_id) {
            await api(`/api/orders/${Number(d.order_id)}/cancel`, {
              method: "POST",
              body: JSON.stringify({})
            }).catch(() => {});
          }
          state.promo = null;
          toast("Payment failed");
          renderCheckout();
        } else {
          renderCheckout();
        }
      });
      return;
    }

    if (d.invoice_url) {
      window.location.href = d.invoice_url;
      return;
    }

    throw new Error("Payment invoice was not created.");
  } catch (e) {
    toast(e.message);

    if (button) {
      button.disabled = false;
      const retryTotal = discountedTotal();
      button.textContent = retryTotal === 0 ? "🎁 Complete Free Order" : `⭐ Pay ${retryTotal} Stars`;
    }
  }
}

async function loadReferral() {
  try {
    const d = await api("/api/referral", {
      method: "POST",
      body: JSON.stringify({})
    });

    state.referral = d.referral || d;
    renderReferral();
  } catch (e) {
    const box = $("referralBox");
    if (box) box.innerHTML = empty(e.message);
  }
}

function renderReferral() {
  const box = $("referralBox");
  if (!box) return;

  const r = state.referral || {};
  const code = r.referral_code || r.code || r.referralCode || "";
  const link = r.referral_link || r.link || "";
  const count = Number(r.referral_count ?? r.referred_count ?? r.referrals ?? 0);
  const earned = Number(r.active_discount_percent ?? r.reward_total_percent ?? 0);
  const per = Number(r.per_referral_percent ?? 0);
  const max = Number(r.max_discount_percent ?? 0);
  const days = Number(r.discount_days ?? 0);

  box.innerHTML = `
    <div class="referral-hero-card">
      <div class="referral-hero-icon">🎁</div>
      <div>
        <span class="eyebrow">REWARDS PROGRAM</span>
        <h2>Invite friends. Earn discount.</h2>
        <p>${esc(state.content?.referral_description || `Share your personal link. Each eligible referral adds ${per}% discount, up to ${max}%.`)}</p>
      </div>
    </div>

    <div class="referral-stat-grid">
      <div class="referral-stat-card"><span>👥 Referrals</span><strong>${count}</strong><small>Successful invitations</small></div>
      <div class="referral-stat-card"><span>🏷️ Your discount</span><strong>${earned}%</strong><small>Currently active</small></div>
      <div class="referral-stat-card"><span>📈 Per referral</span><strong>${per}%</strong><small>Current reward</small></div>
    </div>

    <div class="referral-link-card">
      <div class="referral-card-head"><div><span class="eyebrow">YOUR REFERRAL CODE</span><h3>${esc(code || "Unavailable")}</h3></div><span class="referral-pill">${days > 0 ? `${days} day window` : "No expiry"}</span></div>
      <label>Referral link</label>
      <div class="referral-link-row">
        <input id="referralLink" class="promo-input" value="${esc(link)}" readonly>
        <button class="secondary-btn" id="copyReferral">Copy</button>
      </div>
      <button class="primary-btn referral-share-btn" id="shareReferral">🎁 Share Referral Link</button>
    </div>

    <div class="referral-how">
      <h3>How it works</h3>
      <div class="referral-step"><b>1</b><span>Share your link with a friend.</span></div>
      <div class="referral-step"><b>2</b><span>They join YOUR SHOP through your link.</span></div>
      <div class="referral-step"><b>3</b><span>Your eligible referral reward is added to your discount.</span></div>
    </div>

    <div class="referral-history">
      <div class="section-title"><h3>Recent referrals</h3></div>
      ${
        Array.isArray(r.referrals) && r.referrals.length
          ? r.referrals.slice(0, 10).map(x => `
              <div class="referral-history-row">
                <div><strong>${esc(x.referred_first_name || (x.referred_username ? "@" + x.referred_username : "Customer"))}</strong><small>${esc(formatDate(x.created_at))}</small></div>
                <span>+${Number(x.discount_percent || per)}%</span>
              </div>
            `).join("")
          : `<div class="empty">No referrals yet. Your referral history will appear here.</div>`
      }
    </div>
  `;

  $("copyReferral")?.addEventListener("click", async () => {
    if (!link) return toast("Referral link unavailable");
    try {
      await navigator.clipboard.writeText(link);
      toast("Referral link copied");
    } catch (_) {
      $("referralLink")?.select();
      toast("Select and copy the referral link");
    }
  });

  $("shareReferral")?.addEventListener("click", () => {
    if (!link) return toast("Referral link unavailable");
    const text = "Join YOUR SHOP and check the latest products!";
    const url = `https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent(text)}`;
    if (tg?.openTelegramLink) tg.openTelegramLink(url);
    else window.open(url, "_blank");
  });
}

function formatDate(value) {
  if (!value) return "";
  const d = new Date(value);

  if (Number.isNaN(d.getTime())) {
    return String(value);
  }

  return d.toLocaleString();
}

function statusLabel(status) {
  const map = {
    pending: "Pending",
    delivering: "Delivering",
    processing: "Processing",
    paid: "Paid",
    completed: "Completed",
    cancelled: "Cancelled",
    canceled: "Cancelled",
    failed: "Failed",
    refunded: "Refunded",
    delivered: "Delivered"
  };

  return map[String(status || "").toLowerCase()] ||
    String(status || "Unknown");
}

async function loadOrders() {
  try {
    const d = await api("/api/orders", {
      method: "POST",
      body: JSON.stringify({})
    });

    state.orders = d.orders || [];
    renderOrders();
  } catch (e) {
    const box = $("ordersList");
    if (box) box.innerHTML = empty(e.message);
  }
}

function renderOrders() {
  const box = $("ordersList");
  if (!box) return;
  if (!state.orders.length) {
    box.innerHTML = `<div class="empty-state-card"><div class="empty-icon">🧾</div><strong>No orders yet</strong><span>Your completed and pending purchases will appear here.</span></div>`;
    return;
  }
  box.innerHTML = `
    <div class="orders-grid">
      ${state.orders.map(o => {
        const total = Number(o.total_stars ?? o.total ?? o.amount_stars ?? 0);
        const st = String(o.status || "pending").toLowerCase();
        const qty = Number(o.item_count || o.quantity || 0);
        return `
          <button class="order-tile" data-order="${esc(o.id ?? o.order_id)}" type="button">
            <div class="order-tile-top">
              <span class="order-icon-wrap">🧾</span>
              <span class="status status-${esc(st)}">${esc(statusLabel(st))}</span>
            </div>
            <div class="order-tile-id">Order #${esc(o.id ?? o.order_id)}</div>
            <div class="order-tile-total">⭐ ${total}</div>
            <div class="order-tile-meta">
              <span>${esc(formatDate(o.created_at))}</span>
              ${qty ? `<span>• ${qty} item${qty === 1 ? "" : "s"}</span>` : ""}
            </div>
            <span class="order-view-link">View order details <b>›</b></span>
          </button>`;
      }).join("")}
    </div>`;
}

async function openOrder(id) {
  state.selectedOrderId = id;
  show("orderView");

  const box = $("orderDetails");
  if (!box) return;

  box.innerHTML = empty("Loading order...");

  try {
    const d = await api(
      `/api/orders/${encodeURIComponent(id)}`,
      {
        method: "GET"
      }
    );

    renderOrderDetails(d.order || d);
  } catch (e) {
    box.innerHTML = empty(e.message);
  }
}

async function retryOrderDelivery(orderId) {
  const id = Number(orderId);
  if (!Number.isFinite(id) || id <= 0) {
    toast("Invalid order ID.");
    return;
  }
  const button = document.querySelector(`[data-retry-delivery="${id}"]`);
  if (button) {
    button.disabled = true;
    button.textContent = "Retrying delivery...";
  }
  try {
    await api(`/api/orders/${encodeURIComponent(id)}/delivery/retry`, {
      method: "POST",
      body: JSON.stringify({})
    });
    toast("Delivery completed.");
    await openOrder(id);
  } catch (e) {
    toast(e.message || "Delivery retry failed.");
    await openOrder(id);
  }
}

async function deleteOrder(orderId) {
  const id = Number(orderId);
  if (!Number.isFinite(id) || id <= 0) {
    toast("Invalid order ID.");
    return;
  }
  const confirmed = window.confirm(
    `Remove Order #${id} from My Orders?\n\nThis only hides it from your order list. Your shop/admin order record is kept.`
  );
  if (!confirmed) return;
  try {
    await api(`/api/orders/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("Order removed from My Orders.");
    await loadOrders();
    show("ordersView");
  } catch (e) {
    toast(e.message);
  }
}


function renderOrderDetails(order) {
  const box = $("orderDetails");
  if (!box || !order) return;

  const items = order.items || order.order_items || [];

  const total = Number(
    order.total_stars ??
    order.total ??
    order.amount_stars ??
    0
  );

  box.innerHTML = `
    <div class="order-detail-card">
      <div class="detail-title-row">
        <div>
          <h1>Order #${esc(order.id)}</h1>
          <div class="muted">
            ${esc(formatDate(order.created_at))}
          </div>
        </div>

        <span class="status status-${esc(
          String(order.status || "").toLowerCase()
        )}">
          ${esc(statusLabel(order.status))}
        </span>
      </div>

      <div class="order-items">
        ${
          items.length
            ? items.map((item) => `
              <div class="order-item">
                <div>
                  <strong>${esc(item.name || item.product_name || "Product")}</strong>
                  <div class="muted">
                    Qty: ${Number(item.quantity || 1)}
                  </div>
                </div>

                <strong>
                  ⭐ ${Number(
                    item.total_stars ??
                    item.total ??
                    (
                      Number(item.price_stars || 0) *
                      Number(item.quantity || 1)
                    )
                  )}
                </strong>
              </div>
            `).join("")
            : empty("No order items found.")
        }
      </div>

      ${(() => {
        const delivery = order.delivery || {};
        const deliveryStatus = String(delivery.delivery_status || delivery.status || "").toLowerCase();
        const orderStatus = String(order.status || "").toLowerCase();
        if (["paid", "processing"].includes(orderStatus) && !["delivered", "delivering"].includes(deliveryStatus)) {
          return `<button class="primary-btn" data-retry-delivery="${esc(order.id ?? order.order_id)}">🔄 Retry Delivery</button>`;
        }
        return "";
      })()}

      ${(() => {
        const error = (order.delivery || {}).delivery_error || "";
        return error ? `<div class="checkout-note">⚠️ ${esc(error)}</div>` : "";
      })()}

      <button class="secondary-btn order-delete-btn" id="deleteOrderBtn" data-delete-order="${esc(order.id ?? order.order_id)}">
        🗑️ Remove from My Orders
      </button>

      <div class="summary">
        <div class="sum-row">
          <span>Status</span>
          <span>${esc(statusLabel(order.status))}</span>
        </div>

        <div class="sum-row total">
          <span>Total</span>
          <span>⭐ ${total}</span>
        </div>
      </div>
    </div>
  `;
}

function showSuccess(orderId) {
  show("successView");

  const box = $("successBox");
  if (!box) return;

  box.innerHTML = `
    <div class="success-card">
      <div class="success-icon">✅</div>

      <h1>Order Successful</h1>

      <p class="muted">
        Your order has been received successfully.
      </p>

      ${
        orderId
          ? `
            <div class="success-order">
              Order #${esc(orderId)}
            </div>

            <button
              class="primary-btn"
              id="viewSuccessOrder"
              data-order-success="${esc(orderId)}"
            >
              View Order
            </button>
          `
          : ""
      }

      <button
        class="secondary-btn"
        id="successHomeBtn"
      >
        Continue Shopping
      </button>
    </div>
  `;

  $("viewSuccessOrder")?.addEventListener(
    "click",
    () => openOrder(orderId)
  );

  $("successHomeBtn")?.addEventListener(
    "click",
    () => show("homeView")
  );
}

async function loadTickets() {
  try {
    const d = await api("/api/tickets", {
      method: "POST",
      body: JSON.stringify({})
    });

    state.tickets = d.tickets || [];
    renderTickets();
  } catch (e) {
    const box = $("ticketsList");
    if (box) box.innerHTML = empty(e.message);
  }
}

function ticketStatusLabel(status) {
  const map = {
    open: "Open",
    pending: "Pending",
    waiting: "Waiting for reply",
    in_progress: "In progress",
    closed: "Closed",
    resolved: "Resolved"
  };
  return map[String(status || "").toLowerCase()] || String(status || "Unknown");
}

function ticketCategoryLabel(category) {
  const map = {
    general: "General",
    order: "Order",
    payment: "Payment",
    product: "Product",
    account: "Account",
    technical: "Technical",
    feedback: "Feedback"
  };
  return map[String(category || "").toLowerCase()] || "General";
}

function renderTickets() {
  const box = $("ticketsList");
  if (!box) return;

  const tickets = Array.isArray(state.tickets) ? state.tickets.slice() : [];
  const isClosed = (ticket) => ["closed", "resolved"].includes(String(ticket.status || "").toLowerCase());
  const byUpdated = (a, b) => {
    const ad = Date.parse(a.updated_at || a.created_at || "") || 0;
    const bd = Date.parse(b.updated_at || b.created_at || "") || 0;
    if (bd !== ad) return bd - ad;
    return Number(b.id || 0) - Number(a.id || 0);
  };
  const active = tickets.filter(t => !isClosed(t)).sort(byUpdated);
  const closed = tickets.filter(isClosed).sort(byUpdated);

  const counters = $("ticketCounters");
  if (counters) {
    counters.innerHTML = `
      <span class="ticket-count active">${active.length} active</span>
      <span class="ticket-count closed">${closed.length} closed</span>`;
  }

  if (!tickets.length) {
    box.innerHTML = `
      <div class="support-empty-card">
        <div class="support-empty-icon">🎫</div>
        <span class="eyebrow">NO OPEN REQUESTS</span>
        <h3>No tickets yet</h3>
        <p>Use a ticket for order problems, payments, products, account help, or technical issues. Your previous conversations will stay here for reference.</p>
        <button type="button" class="primary-btn" onclick="document.getElementById('newTicketBtn')?.click()">Open New Ticket</button>
      </div>`;
    return;
  }

  const icons = {
    general: "💬",
    order: "🧾",
    payment: "💳",
    product: "📦",
    account: "👤",
    technical: "🛠️",
    feedback: "⭐"
  };

  const priorityClass = (priority) => {
    const value = String(priority || "normal").toLowerCase();
    return ["low", "normal", "high", "urgent"].includes(value) ? value : "normal";
  };

  const card = (ticket) => {
    const closedTicket = isClosed(ticket);
    const status = String(ticket.status || "open").toLowerCase();
    const priority = priorityClass(ticket.priority);
    const count = Number(ticket.message_count || 0);
    const category = String(ticket.category || "general").toLowerCase();
    const preview = String(ticket.first_message || "Support request").trim();
    return `
      <button type="button" class="ticket-pro-card ${closedTicket ? "closed-card" : ""}" data-ticket="${esc(ticket.id)}">
        <div class="ticket-icon">${icons[category] || "🎫"}</div>
        <div class="ticket-main">
          <div class="ticket-id-row">
            <span class="ticket-id">#${esc(ticket.id)}</span>
            <span class="ticket-priority ${priority}">${esc(priority.charAt(0).toUpperCase() + priority.slice(1))}</span>
          </div>
          <div class="ticket-title">${esc(ticket.subject || "Untitled ticket")}</div>
          <div class="ticket-preview-line">${esc(preview)}</div>
          <div class="ticket-meta">
            <span>◉ ${esc(ticketCategoryLabel(category))}</span>
            <span>▱ ${count} message${count === 1 ? "" : "s"}</span>
            <span>◷ ${esc(formatDate(ticket.updated_at || ticket.created_at))}</span>
          </div>
        </div>
        <div class="ticket-side">
          <span class="ticket-status-pill ${closedTicket ? "closed" : ""}">● ${esc(ticketStatusLabel(status))}</span>
          <span class="ticket-action">${closedTicket ? "View" : "Open"} ›</span>
        </div>
      </button>`;
  };

  let html = "";
  if (active.length) {
    html += `<div class="ticket-group-label"><span>ACTIVE TICKETS</span><b>${active.length}</b></div>`;
    html += active.map(card).join("");
  }
  if (closed.length) {
    html += `<div class="ticket-group-label closed"><span>CLOSED & RESOLVED</span><b>${closed.length}</b></div>`;
    html += closed.map(card).join("");
  }
  box.innerHTML = html;
}

function renderTicketComposer() {
  const box = $("ticketComposer");
  if (!box) return;

  box.innerHTML = `
    <div class="ticket-compose-card premium-ticket-compose">
      <div class="support-form-head"><div class="support-form-icon">🎫</div><div><span class="eyebrow">HELP DESK</span><h2>Open a Support Ticket</h2><p class="muted">Give us enough detail to solve the issue without a second interrogation. Humanity has enough forms already.</p></div></div>

      <label class="support-field-label">Issue category
        <select id="ticketCategory" class="promo-input">
          <option value="general">General</option>
          <option value="order">Order</option>
          <option value="payment">Payment</option>
          <option value="product">Product</option>
          <option value="account">Account</option>
          <option value="technical">Technical</option>
          <option value="feedback">Feedback</option>
        </select>
      </label>

      <label class="support-field-label">Priority
        <select id="ticketPriority" class="promo-input">
          <option value="normal">Normal</option>
          <option value="high">High</option>
          <option value="urgent">Urgent</option>
          <option value="low">Low</option>
        </select>
      </label>

      <label class="support-field-label">Subject
        <input id="ticketSubject" class="promo-input" placeholder="Short description of the issue" maxlength="120">
      </label>

      <label class="support-field-label">Order ID <span class="muted">(optional)</span>
        <input id="ticketOrderId" class="promo-input" inputmode="numeric" placeholder="Example: 125">
      </label>

      <label class="support-field-label">Message
        <textarea id="ticketMessage" class="promo-input" rows="7" placeholder="Describe what happened, what you expected, and any useful order/product details..." maxlength="5000"></textarea>
      </label>

      <button id="submitTicket" class="primary-btn">🎫 Send Ticket</button>
    </div>
  `;

  $("submitTicket")?.addEventListener("click", submitTicket);
}

async function submitTicket() {
  const subject = String(
    $("ticketSubject")?.value || ""
  ).trim();

  const message = String(
    $("ticketMessage")?.value || ""
  ).trim();

  if (!subject) {
    return toast("Please enter a subject");
  }

  if (!message) {
    return toast("Please enter your message");
  }

  const button = $("submitTicket");

  if (button) {
    button.disabled = true;
    button.textContent = "Sending...";
  }

  try {
    const d = await api("/api/tickets/create", {
      method: "POST",
      body: JSON.stringify({
        subject,
        message,
        category: $("ticketCategory")?.value || "general",
        priority: $("ticketPriority")?.value || "normal",
        order_id: $("ticketOrderId")?.value || null
      })
    });

    const ticket = d.ticket || d;

    await loadTickets();
    state.selectedTicketId = ticket.id;

    toast("Ticket created");
    await openTicket(ticket.id);
  } catch (e) {
    toast(e.message);

    if (button) {
      button.disabled = false;
      button.textContent = "Send Ticket";
    }
  }
}

async function openTicket(id) {
  state.selectedTicketId = id;
  show("ticketView");

  const box = $("ticketDetails");
  if (!box) return;

  box.innerHTML = empty("Loading ticket...");

  try {
    const d = await api(
      `/api/tickets/${encodeURIComponent(id)}`,
      {
        method: "POST",
        body: JSON.stringify({})
      }
    );

    renderTicketDetails(d.ticket || d);
  } catch (e) {
    box.innerHTML = empty(e.message);
  }
    }
function renderTicketDetails(ticket) {
  const box = $("ticketDetails");
  if (!box || !ticket) return;

  const messages = ticket.messages || ticket.replies || ticket.ticket_messages || [];
  const status = String(ticket.status || "open").toLowerCase();
  const closed = ["closed", "resolved"].includes(status);
  const category = ticketCategoryLabel(ticket.category);
  const priority = String(ticket.priority || "normal").toLowerCase();

  box.innerHTML = `
    <button type="button" class="ticket-detail-back" id="ticketBackToSupport">‹ Back to support</button>
    <div class="ticket-detail-card support-ticket-detail">
      <div class="ticket-detail-top">
        <div class="ticket-detail-title-wrap">
          <span class="eyebrow">SUPPORT TICKET #${esc(ticket.id)}</span>
          <h1>${esc(ticket.subject || `Ticket #${ticket.id}`)}</h1>
          <p class="ticket-detail-subtitle">Created ${esc(formatDate(ticket.created_at))}</p>
        </div>
        <span class="ticket-status-pill ${closed ? "closed" : ""}">● ${esc(ticketStatusLabel(status))}</span>
      </div>
      <div class="ticket-detail-meta">
        <span>◉ ${esc(category)}</span>
        <span class="priority-${esc(priority)}">${esc(priority.charAt(0).toUpperCase() + priority.slice(1))} priority</span>
        ${ticket.order_id ? `<span>🧾 Order #${esc(ticket.order_id)}</span>` : ""}
      </div>

      <div class="ticket-conversation-head"><div><strong>Conversation</strong><span>${messages.length} message${messages.length === 1 ? "" : "s"}</span></div></div>
      <div class="ticket-messages">
        ${messages.length ? messages.map((m) => {
          const admin = m.is_admin || m.author_type === "admin" || !m.telegram_id;
          return `
            <div class="ticket-bubble ${admin ? "admin" : "user"}">
              <div class="ticket-bubble-head"><strong>${admin ? "YOUR SHOP Support" : "You"}</strong><small>${esc(formatDate(m.created_at))}</small></div>
              <div class="ticket-bubble-body">${esc(m.message || m.body || "")}</div>
            </div>`;
        }).join("") : `<div class="support-empty-mini">No messages yet.</div>`}
      </div>

      ${closed ? `
        <div class="ticket-closed-note"><strong>This ticket is ${esc(ticketStatusLabel(status).toLowerCase())}.</strong><span>Open a new ticket for a new issue.</span></div>
      ` : `
        <div class="ticket-reply-card">
          <div class="ticket-reply-head"><div><strong>Reply to support</strong><span>Keep all details in this conversation.</span></div></div>
          <textarea id="ticketReplyInput" class="promo-input" rows="5" placeholder="Write your reply…" maxlength="5000"></textarea>
          <button id="sendTicketReply" class="primary-btn">Send Reply</button>
        </div>
      `}
    </div>
  `;

  $("ticketBackToSupport")?.addEventListener("click", async () => {
    state.selectedTicketId = null;
    await loadTickets();
    show("supportView");
  });

  $("sendTicketReply")?.addEventListener("click", sendTicketReply);
}

async function sendTicketReply() {
  const id = state.selectedTicketId;

  if (!id) {
    return toast("Ticket not selected");
  }

  const input = $("ticketReplyInput");
  const message = String(
    input?.value || ""
  ).trim();

  if (!message) {
    return toast("Write a message first");
  }

  const button = $("sendTicketReply");

  if (button) {
    button.disabled = true;
    button.textContent = "Sending...";
  }

  try {
    await api(
      `/api/tickets/${encodeURIComponent(id)}/reply`,
      {
        method: "POST",
        body: JSON.stringify({ message })
      }
    );

    await openTicket(id);
    toast("Reply sent");
  } catch (e) {
    toast(e.message);

    if (button) {
      button.disabled = false;
      button.textContent = "Send Reply";
    }
  }
}

/* ---------------- Notifications ---------------- */

async function loadNotifications() {
  try {
    const d = await api("/api/notifications", {
      method: "POST",
      body: JSON.stringify({})
    });

    state.notifications = d.notifications || [];
    state.notificationsUnread = Number(
      d.unread_count || 0
    );

    renderNotifications();
    updateNotificationBadge();
  } catch (e) {
    /*
     * Notifications are optional.
     * If the backend endpoint does not exist yet,
     * the rest of the shop must continue to work.
     */
    console.warn("Notifications load failed:", e.message);
  }
}

function updateNotificationBadge() {
  const badge =
    $("notificationBadge") ||
    $("notificationsBadge");

  if (!badge) return;

  badge.textContent = String(
    state.notificationsUnread || 0
  );

  badge.hidden = state.notificationsUnread <= 0;
}

function renderNotifications() {
  const box =
    $("notificationsList") ||
    $("notificationsBox");

  if (!box) return;

  if (!state.notifications.length) {
    box.innerHTML = empty(
      "You have no notifications."
    );
    return;
  }

  box.innerHTML = state.notifications.map((n) => {
    const id = n.id;
    const unread =
      !n.read &&
      !n.is_read &&
      !n.read_at;

    return `
      <button
        class="notification-card ${unread ? "unread" : ""}"
        data-notification="${esc(id)}"
      >
        <div class="notification-icon">
          ${unread ? "🔔" : "✅"}
        </div>

        <div class="notification-content">
          <strong>
            ${esc(n.title || "Notification")}
          </strong>

          <p>
            ${esc(n.message || n.body || "")}
          </p>

          <small>
            ${esc(formatDate(n.created_at || n.date))}
          </small>
        </div>
      </button>
    `;
  }).join("");
}

async function markNotificationRead(id) {
  try {
    await api(
      `/api/notifications/${encodeURIComponent(id)}/read`,
      {
        method: "POST",
        body: JSON.stringify({})
      }
    );

    const item = state.notifications.find(
      (n) => String(n.id) === String(id)
    );

    if (item) {
      item.read = true;
      item.is_read = true;
    }

    state.notificationsUnread = Math.max(
      0,
      state.notificationsUnread - 1
    );

    renderNotifications();
    updateNotificationBadge();
  } catch (e) {
    toast(e.message);
  }
}

async function loadProductReviews(productId) {
  const box=$("productReviews"); if(!box) return;
  try {
    const d=await api(`/api/products/${encodeURIComponent(productId)}/reviews`);
    const rows=d.reviews||[];
    box.innerHTML=`<div class="section-title"><h3>⭐ Reviews (${rows.length})</h3></div>
      <div class="review-list">${rows.length?rows.map(r=>`<article class="review-card"><div class="review-head"><strong>${esc(r.first_name||r.username||"Customer")}</strong><span>${"★".repeat(Math.max(1,Math.min(5,Number(r.rating||5))))}</span></div><p>${esc(r.text||"")}</p><small>Helpful: ${Number(r.helpful||0)}</small><button class="text-btn" data-review-vote="${esc(r.id)}">Helpful</button></article>`).join(""):`<div class="empty">No reviews yet. Be the first.</div>`}</div>
      <div class="review-form"><h4>Leave a free review</h4><div class="rating-picker" id="reviewRating">${[1,2,3,4,5].map(i=>`<button data-review-rating="${i}">★</button>`).join("")}</div><textarea id="reviewText" class="promo-input" rows="4" maxlength="3000" placeholder="Write your review..."></textarea><div class="review-actions"><button id="submitReview" class="primary-btn">Publish Free Review</button><button id="submitPaidReview" class="secondary-btn">⭐ Paid Review</button></div></div>`;
    let rr=5; document.querySelectorAll("[data-review-rating]").forEach(b=>b.addEventListener("click",()=>{rr=Number(b.dataset.reviewRating);document.querySelectorAll("[data-review-rating]").forEach(x=>x.classList.toggle("active",Number(x.dataset.reviewRating)<=rr));}));
    $("submitReview")?.addEventListener("click",async()=>{const text=String($("reviewText")?.value||"").trim();if(!text)return toast("Write a review first");try{await api(`/api/products/${productId}/reviews`,{method:"POST",body:JSON.stringify({rating:rr,text,review_type:"free"})});toast("Review published");loadProductReviews(productId);}catch(e){toast(e.message);}});
    document.querySelectorAll("[data-review-vote]").forEach(b=>b.addEventListener("click",async()=>{try{await api(`/api/reviews/${b.dataset.reviewVote}/vote`,{method:"POST",body:JSON.stringify({vote:1})});toast("Thanks for your vote");loadProductReviews(productId);}catch(e){toast(e.message);}}));
    $("submitPaidReview")?.addEventListener("click",async()=>{const text=String($("reviewText")?.value||"").trim();if(!text)return toast("Write a review first");try{const d=await api(`/api/products/${productId}/reviews`,{method:"POST",body:JSON.stringify({rating:rr,text,review_type:"paid",price_stars:10})});if(d.invoice_url&&tg?.openInvoice){tg.openInvoice(d.invoice_url,(status)=>{if(status==="paid"){toast("Paid review payment complete");loadProductReviews(productId);}else if(status==="failed"){toast("Paid review payment failed");}});}else if(d.invoice_url){window.open(d.invoice_url,"_blank");}else{toast("Paid review invoice unavailable");}}catch(e){toast(e.message);}});
  } catch(e) { box.innerHTML=empty("Reviews are temporarily unavailable."); }
}

async function loadGiveaways() {
  const box = $("giveawaysList");
  if (!box) return;
  box.innerHTML = empty("Loading giveaways...");
  try {
    const d = await api("/api/giveaways");
    const rows = d.giveaways || [];
    box.innerHTML = rows.length ? rows.map(g => {
      const shareText = `🎉 ${String(g.title || "YOUR SHOP Giveaway")}\n\n${String(g.description || "Join this YOUR SHOP giveaway!")}`;
      const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(window.location.href)}&text=${encodeURIComponent(shareText)}`;
      return `<article class="giveaway-card"><div class="giveaway-icon">🎉</div><div><h3>${esc(g.title)}</h3><p>${esc(g.description||"")}</p><div class="muted">👥 ${Number(g.entry_count||0)} entries · 🏆 ${Number(g.winner_count||1)} winners</div><button class="primary-btn" data-join-giveaway="${esc(g.id)}">Join Giveaway</button><button class="secondary-btn" data-share-giveaway="${esc(shareUrl)}">📤 Share Giveaway</button></div></article>`;
    }).join("") : empty("No active giveaways right now.");

    document.querySelectorAll("[data-join-giveaway]").forEach(b => b.addEventListener("click", async () => {
      try {
        const r = await api(`/api/giveaways/${b.dataset.joinGiveaway}/join`, {method:"POST", body:JSON.stringify({})});
        toast(r.message || "Joined");
        loadGiveaways();
      } catch (e) { toast(e.message); }
    }));

    document.querySelectorAll("[data-share-giveaway]").forEach(b => b.addEventListener("click", () => {
      const url = String(b.dataset.shareGiveaway || "");
      if (tg?.openTelegramLink && url) tg.openTelegramLink(url);
      else if (url) window.open(url, "_blank");
    }));
  } catch (e) {
    box.innerHTML = empty(e.message);
  }
}

function promoTargetLabel(p){
  const type=String(p?.target_type||"global").toLowerCase();
  if(type==="global") return "All products";
  const name=String(p?.target_name||"").trim();
  if(type==="game") return `Game: ${name||`#${p.target_id||"-"}`}`;
  if(type==="category") return `Category: ${name||`#${p.target_id||"-"}`}`;
  if(type==="product") return `Product: ${name||`#${p.target_id||"-"}`}`;
  return "Selected target";
}

async function loadPromos(){
  const box=$("promoList"); if(!box)return;
  box.innerHTML=empty("Loading promo codes...");
  try{
    const d=await api("/api/promo-codes");
    const rows=d.promo_codes||[];
    if(!rows.length){box.innerHTML=empty("No promo codes are currently available.");return;}
    const groups={global:[],game:[],category:[],product:[]};
    rows.forEach(p=>{const key=String(p.target_type||"global").toLowerCase();(groups[key]||groups.global).push(p)});
    const section=(title,arr)=>arr.length?`<div class="promo-group"><h2 style="margin:18px 0 10px;font-size:17px">${esc(title)}</h2>${arr.map(p=>`<article class="promo-card">
      <span class="featured-pill">AVAILABLE</span>
      <h3>🎟️ Promo Code</h3>
      <p>${Number(p.discount_percent||0)?`Save ${Number(p.discount_percent)}%`:`Save ⭐ ${Number(p.discount_stars||0)}`}</p>
      <p class="muted">🎯 ${esc(promoTargetLabel(p))}</p>
      ${p.description?`<p class="muted">${esc(p.description)}</p>`:""}
      <div class="muted">Minimum eligible items: ${Number(p.min_cart_quantity||1)}</div>
      <div class="muted">Price: ⭐ ${Number(p.sale_price_stars||0)} Stars</div>
      <div class="muted">Expires: ${esc(formatDate(p.expires_at)||"Never")}</div>
      <button class="primary-btn" data-buy-promo="${esc(p.id)}">⭐ Buy Promo Code</button>
    </article>`).join("")}</div>`:"";
    box.innerHTML=section("🌐 All Products",groups.global)+section("🎮 By Game",groups.game)+section("🗂️ By Category",groups.category)+section("📦 By Product",groups.product);
    document.querySelectorAll("[data-buy-promo]").forEach(btn=>btn.addEventListener("click",async()=>{
      btn.disabled=true; btn.textContent="Creating invoice...";
      try{
        const r=await api("/api/promo-codes/purchase",{method:"POST",body:JSON.stringify({promo_id:Number(btn.dataset.buyPromo)})});
        if(r.invoice_url&&tg?.openInvoice){
          tg.openInvoice(r.invoice_url,async status=>{
            if(status==="paid"){toast("Promo code purchased successfully");await loadPromos();}
            else if(status==="failed"){toast("Promo code payment failed");btn.disabled=false;btn.textContent="⭐ Buy Promo Code";}
            else if(status==="cancelled"){btn.disabled=false;btn.textContent="⭐ Buy Promo Code";}
          });
        }else if(r.invoice_url){window.location.href=r.invoice_url;}
        else throw new Error("Payment invoice was not created.");
      }catch(e){toast(e.message);btn.disabled=false;btn.textContent="⭐ Buy Promo Code";}
    }));
  }catch(e){box.innerHTML=empty(e.message);}
}

async function renderCurrentProductDetail() {
  if (!state.selectedProductId) return;

  try {
    const d = await api(
      `/api/products/${encodeURIComponent(
        state.selectedProductId
      )}`
    );

    renderDetail(d.product);
  } catch (e) {
    console.error(
      "Product detail refresh failed:",
      e
    );
  }
}

/* ---------------- Event handling ---------------- */

function setupShopEvents() {
  document.addEventListener("click", async (e) => {
    const favorite = e.target.closest(
      "[data-favorite]"
    );

    if (favorite) {
      e.preventDefault();
      e.stopPropagation();

      await toggleFavorite(
        favorite.dataset.favorite
      );
      return;
    }

    const game = e.target.closest("[data-game]");

    if (game) {
      state.gameId = game.dataset.game;
      state.categoryId = null;
      state.search = "";

      try {
        await loadGames();
        await loadCategories();
        await loadProducts();
      } catch (err) {
        toast(err.message);
      }

      return;
    }

    const category = e.target.closest(
      "[data-category]"
    );

    if (category) {
      const value = category.dataset.category;

      state.categoryId =
        value === "all"
          ? null
          : Number(value);

      try {
        await loadCategories();
        await loadProducts();
      } catch (err) {
        toast(err.message);
      }

      return;
    }

    const add = e.target.closest("[data-add]");

    if (add) {
      e.preventDefault();
      e.stopPropagation();

      if (add.disabled) return;

      await addCart(add.dataset.add);
      return;
    }

    const qty = e.target.closest("[data-qty]");

    if (qty) {
      e.preventDefault();

      const delta =
        qty.dataset.qty === "+"
          ? 1
          : -1;

      await changeQty(
        qty.dataset.id,
        delta
      );
      return;
    }

    const remove = e.target.closest(
      "[data-remove]"
    );

    if (remove) {
      e.preventDefault();
      await removeCart(
        remove.dataset.remove
      );
      return;
    }

    const product = e.target.closest(
      "[data-product]"
    );

    if (product) {
      e.preventDefault();

      await openProduct(
        product.dataset.product
      );
      return;
    }

    const ticket = e.target.closest(
      "[data-ticket]"
    );

    if (ticket) {
      e.preventDefault();

      await openTicket(
        ticket.dataset.ticket
      );
      return;
    }

    const retryDeliveryButton = e.target.closest("[data-retry-delivery]");

    if (retryDeliveryButton) {
      e.preventDefault();
      e.stopPropagation();
      await retryOrderDelivery(retryDeliveryButton.dataset.retryDelivery);
      return;
    }

    const deleteOrderButton = e.target.closest("[data-delete-order]");

    if (deleteOrderButton) {
      e.preventDefault();
      e.stopPropagation();
      await deleteOrder(deleteOrderButton.dataset.deleteOrder);
      return;
    }

    const order = e.target.closest(
      "[data-order]"
    );

    if (order) {
      e.preventDefault();

      await openOrder(
        order.dataset.order
      );
      return;
    }

    const notification = e.target.closest(
      "[data-notification]"
    );

    if (notification) {
      e.preventDefault();

      await markNotificationRead(
        notification.dataset.notification
      );

      return;
    }

    const nav = e.target.closest("[data-nav]");

    if (nav) {
      e.preventDefault();

      const target =
        nav.dataset.nav;

      if (target === "cartView") {
        await loadCart().catch(() => {});
      }

      if (target === "favoritesView") {
        await loadFavorites();
      }

      if (target === "ordersView") {
        await loadOrders();
      }

      if (target === "referralView") {
        await loadReferral();
      }

      if (target === "supportView") {
        await loadTickets();
      }

      if (target === "notificationsView") { await loadNotifications(); }
      if (target === "giveawaysView") { await loadGiveaways(); }
      if (target === "promoView") { await loadPromos(); }

      show(target);
      return;
    }

    const back = e.target.closest(
      "[data-back]"
    );

    if (back) {
      e.preventDefault();

      show(
        back.dataset.back ||
        "homeView"
      );

      return;
    }
  });

  let feedbackRating=5;
  document.querySelectorAll("[data-rating]").forEach(b=>b.addEventListener("click",()=>{feedbackRating=Number(b.dataset.rating);document.querySelectorAll("[data-rating]").forEach(x=>x.classList.toggle("active",Number(x.dataset.rating)<=feedbackRating));}));
  $("sendFeedback")?.addEventListener("click",async()=>{const text=String($("feedbackInput")?.value||"").trim();if(!text)return toast("Write your feedback first");try{await api("/api/feedback",{method:"POST",body:JSON.stringify({message:text,rating:feedbackRating})});$("feedbackInput").value="";toast("Feedback sent. Thank you!");show("homeView");}catch(e){toast(e.message);}});

  $("notificationsBtn")?.addEventListener("click",async()=>{await loadNotifications();show("notificationsView");});
  $("markAllNotifications")?.addEventListener("click",async()=>{try{await api("/api/notifications/read-all",{method:"POST",body:JSON.stringify({})});state.notifications.forEach(n=>{n.read=true;n.is_read=true;});state.notificationsUnread=0;renderNotifications();updateNotificationBadge();}catch(e){toast(e.message);}});
  $("openSupportHome")?.addEventListener("click",async()=>{await loadTickets();show("supportView");});
  $("refreshGiveaways")?.addEventListener("click",loadGiveaways);
  $("refreshPromos")?.addEventListener("click",loadPromos);

  $("cartBtn")?.addEventListener(
    "click",
    async () => {
      await loadCart();
      show("cartView");
    }
  );

  $("showAllProducts")?.addEventListener(
    "click",
    async () => {
      state.gameId = null;
      state.categoryId = null;
      state.search = "";

      try {
        await loadGames();
        await loadCategories();
        await loadProducts();
      } catch (e) {
        toast(e.message);
      }

      show("homeView");
    }
  );

  const searchInput = $("searchInput");

  if (searchInput) {
    let searchTimer = null;

    searchInput.addEventListener(
      "input",
      () => {
        clearTimeout(searchTimer);

        searchTimer = setTimeout(() => {
          state.search =
            String(searchInput.value || "");

          renderProducts();
        }, 120);
      }
    );
  }

  $("refreshOrders")?.addEventListener(
    "click",
    () => loadOrders()
  );

  $("refreshReferral")?.addEventListener(
    "click",
    () => loadReferral()
  );

  $("refreshTickets")?.addEventListener(
    "click",
    () => loadTickets()
  );

  $("refreshNotifications")?.addEventListener(
    "click",
    () => loadNotifications()
  );

  $("newTicketBtn")?.addEventListener(
    "click",
    () => {
      renderTicketComposer();
      show("newTicketView");
    }
  );

  $("openSupportBtn")?.addEventListener(
    "click",
    async () => {
      await loadTickets();
      show("supportView");
    }
  );

  $("successHomeBtn")?.addEventListener(
    "click",
    () => show("homeView")
  );
}

/* ---------------- Initialisation ---------------- */

async function init() {
  try {
    await loadPublicContent();
  } catch (e) {
    console.error("Public content initialization failed:", e);
  }
  try {
    await loadGames();
  } catch (e) {
    console.error("Games load failed:", e);
  }

  try {
    await loadCategories();
  } catch (e) {
    console.error(
      "Categories load failed:",
      e
    );
  }

  try {
    await loadProducts();
  } catch (e) {
    console.error(
      "Products load failed:",
      e
    );
  }

  try {
    await loadFavorites();
  } catch (e) {
    console.error(
      "Favorites load failed:",
      e
    );
  }

  try {
    await loadCart();
  } catch (e) {
    console.error(
      "Cart load failed:",
      e
    );
  }

  try {
    await loadNotifications();
  } catch (e) {
    console.error(
      "Notifications load failed:",
      e
    );
  }
}

document.addEventListener(
  "DOMContentLoaded",
  () => {
    setupShopEvents();
    init();
  }
);
