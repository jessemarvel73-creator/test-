"use strict";

/* YOUR SHOP Loyalty Rewards module.
   Add this script after app.js, or merge these functions into the existing app.js. */

window.CPMLoyalty = {
  async load() {
    const box = document.getElementById("loyaltyBox");
    if (box) box.innerHTML = '<div class="empty">Loading rewards...</div>';

    try {
      const tg = window.Telegram?.WebApp;
      const initData = tg?.initData || "";

      const response = await fetch("/api/loyalty", {
        method: "POST",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify({ init_data: initData })
      });

      const data = await response.json();
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || "Unable to load rewards.");
      }

      this.render(data);
      return data;
    } catch (error) {
      console.error("[LOYALTY]", error);
      if (box) box.innerHTML = `<div class="empty error">${this.escape(error.message)}</div>`;
      return null;
    }
  },

  escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  },

  render(data) {
    const box = document.getElementById("loyaltyBox");
    if (!box) return;

    const history = Array.isArray(data.history) ? data.history : [];

    box.innerHTML = `
      <div class="loyalty-card">
        <div class="loyalty-hero">
          <div class="loyalty-icon">⭐</div>
          <div>
            <h2>Loyalty Rewards</h2>
            <p>Earn 1 reward point for every paid Star on a completed purchase.</p>
          </div>
        </div>

          <div class="loyalty-stats loyalty-stats-grid">
          <div class="stat-card"><span>Balance</span><strong>${Number(data.balance || 0)} ⭐</strong></div>
          <div class="stat-card"><span>Total Earned</span><strong>${Number(data.earned || 0)} ⭐</strong></div>
          <div class="stat-card"><span>Total Spent</span><strong>${Number(data.spent || 0)} ⭐</strong></div>
        </div>

        <div class="loyalty-history">
          <h3>Reward History</h3>
          ${history.length ? history.map(item => `
            <div class="loyalty-row">
              <div>
                <strong>${this.escape(item.reason || "Reward")}</strong>
                <span>${this.escape(item.reference_type === "order" && item.reference_id ? `Order #${item.reference_id}` : "")}</span>
              </div>
              <div>
                <strong class="reward-positive">+${Number(item.amount || 0)} ⭐</strong>
                <span>${this.escape(item.created_at || "")}</span>
              </div>
            </div>
          `).join("") : '<div class="empty">No rewards yet. Complete a paid order to start earning.</div>'}
        </div>
      </div>
    `;
  }
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => {
    if (document.getElementById("loyaltyBox")) window.CPMLoyalty.load();
  }, { once: true });
} else if (document.getElementById("loyaltyBox")) {
  window.CPMLoyalty.load();
}

/* CSS helper text intentionally omitted from JS module. */
