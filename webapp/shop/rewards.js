"use strict";

/*
 * YOUR SHOP
 * Loyalty Redemption + VIP Rewards
 *
 * Safe standalone module for the existing 834-line app.js.
 *
 * IMPORTANT:
 * - Does NOT override window.fetch
 * - Does NOT override Telegram.WebApp.openInvoice
 * - Does NOT modify Telegram WebApp internals
 * - Does NOT replace app.js payment logic
 * - Sends the selected redemption ID through CPMRewards API
 */

(function () {
  let tg = null;
  let selectedRedemption = null;
  let checkoutObserver = null;
  let checkoutObserverTimer = null;
  let initialized = false;

  /*
   * Telegram WebApp
   */
  function initTelegram() {
    tg = window.Telegram?.WebApp || null;

    if (tg) {
      try {
        tg.ready();
        tg.expand();
      } catch (error) {
        console.warn(
          "[REWARDS] Telegram initialization warning:",
          error
        );
      }
    }

    return tg;
  }

  initTelegram();

  /*
   * Helpers
   */

  const $ = (id) =>
    document.getElementById(id);

  const initData = () =>
    tg?.initData || "";

  const escapeHTML = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");

  function showToast(message) {
    if (
      typeof window.toast === "function"
    ) {
      window.toast(message);
      return;
    }

    const box = $("toast");

    if (!box) {
      return;
    }

    box.textContent = message;
    box.classList.add("show");

    clearTimeout(
      box._rewardTimer
    );

    box._rewardTimer =
      setTimeout(() => {
        box.classList.remove("show");
      }, 2200);
  }

  /*
   * API
   */

  async function request(
    url,
    body = {}
  ) {
    const response = await fetch(
      url,
      {
        method: "POST",
        cache: "no-store",
        headers: {
          "Content-Type":
            "application/json",
          Accept:
            "application/json"
        },
        body: JSON.stringify({
          init_data: initData(),
          ...body
        })
      }
    );

    let data = {};

    try {
      data = await response.json();
    } catch (_) {
      data = {};
    }

    if (
      !response.ok ||
      data.ok === false
    ) {
      throw new Error(
        data.error ||
          `Request failed (${response.status})`
      );
    }

    return data;
  }

  /*
   * Loyalty information
   */

  async function loadLoyalty() {
    try {
      const data =
        await request(
          "/api/loyalty"
        );

      renderRewards(data);

      return data;
    } catch (error) {
      console.error(
        "[REWARDS] Loyalty load failed:",
        error
      );

      renderRewards({
        balance: 0,
        available: 0,
        earned: 0,
        spent: 0,
        reserved: 0,
        level: "Bronze",
        next_level_points: 500,
        level_progress: 0
      });

      return null;
    }
  }

  /*
   * Render rewards section
   */

  function renderRewards(data) {
    const loyaltyBox =
      $("loyaltyBox");

    if (!loyaltyBox) {
      return;
    }

    const old =
      $("rewardsActions");

    if (old) {
      old.remove();
    }

    const available =
      Number(
        data.available ??
          data.balance ??
          0
      );

    const earned =
      Number(
        data.earned || 0
      );

    const spent =
      Number(
        data.spent || 0
      );

    const reserved =
      Number(
        data.reserved || 0
      );

    const level =
      String(
        data.level ||
          "Bronze"
      );

    const progress =
      Math.max(
        0,
        Math.min(
          100,
          Number(
            data.level_progress ||
              0
          )
        )
      );

    const next =
      Number(
        data.next_level_points ||
          0
      );

    const panel =
      document.createElement(
        "section"
      );

    panel.id =
      "rewardsActions";

    panel.className =
      "rewards-panel";

    panel.innerHTML = `
      <div class="rewards-head">

        <div>
          <div class="rewards-kicker">
            REWARDS
          </div>

          <h3>
            Redeem points for Star discounts
          </h3>

          <p>
            Use your earned points at checkout.
            One redemption is reserved for 30 minutes.
          </p>
        </div>

        <div class="rewards-level">
          ${escapeHTML(level)}
        </div>

      </div>

      <div class="rewards-progress-wrap">

        <div class="rewards-progress">
          <span
            style="width:${progress}%"
          ></span>
        </div>

        <div class="rewards-progress-text">

          <span>
            ${available} available points
          </span>

          <span>
            ${
              next > 0
                ? `${next} points for next level`
                : "Highest level"
            }
          </span>

        </div>

      </div>

      <div class="rewards-stats">
      
        <div>
          <span>Earned</span>
          <strong>${earned}</strong>
        </div>

        <div>
          <span>Spent</span>
          <strong>${spent}</strong>
        </div>

        <div>
          <span>Reserved</span>
          <strong>${reserved}</strong>
        </div>

      </div>

      <div class="rewards-options">

        ${rewardButton(
          100,
          1,
          available
        )}

        ${rewardButton(
          500,
          5,
          available
        )}

        ${rewardButton(
          1000,
          10,
          available
        )}

      </div>

      <div class="rewards-note">
        Current lifetime earned:
        <strong>${earned}</strong>
        points.
      </div>

      <div id="selectedRewardSlot"></div>
    `;

    loyaltyBox.appendChild(
      panel
    );

    renderSelectedReward();
  }

  /*
   * Reward buttons
   */

  function rewardButton(
    points,
    stars,
    available
  ) {
    const disabled =
      available < points;

    return `
      <button
        class="reward-option"
        type="button"
        data-redeem-points="${points}"
        ${disabled ? "disabled" : ""}
      >

        <strong>
          ${points} points
        </strong>

        <span>
          −⭐ ${stars}
        </span>

      </button>
    `;
  }

  /*
   * Redeem reward
   */

  async function redeem(
    points
  ) {
    const button =
      document.querySelector(
        `[data-redeem-points="${points}"]`
      );

    if (button) {
      button.disabled = true;
      button.dataset.loading =
        "true";
    }

    try {
      /*
       * Cancel previous reward first.
       */
      if (
        selectedRedemption
      ) {
        const cancelled =
          await cancelSelected(
            false
          );

        if (!cancelled) {
          throw new Error(
            "The previous reward could not be removed."
          );
        }
      }

      /*
       * Reserve new reward.
       */
      const data =
        await request(
          "/api/loyalty/redeem",
          {
            points:
              Number(points)
          }
        );

      selectedRedemption =
        data.redemption ||
        null;

      renderSelectedReward();

      if (typeof window.CPMShopRenderCheckout === "function") {
        window.CPMShopRenderCheckout();
      }

      showToast(
        data.message ||
          "Reward reserved for checkout."
      );

      /*
       * Refresh available points.
       */
      await loadLoyalty();

    } catch (error) {
      console.error(
        "[REWARDS] Redeem failed:",
        error
      );

      showToast(
        error.message ||
          "Unable to redeem reward."
      );

      if (button) {
        button.disabled = false;
        delete button.dataset.loading;
      }
    }
  }

  /*
   * Cancel selected reward
   */

  async function cancelSelected(
    showMessage = true
  ) {
    const current =
      selectedRedemption;

    if (!current) {
      return true;
    }

    try {
      await request(
        "/api/loyalty/redeem/cancel",
        {
          redemption_id:
            Number(current.id)
        }
      );

      selectedRedemption =
        null;

      renderSelectedReward();

      if (typeof window.CPMShopRenderCheckout === "function") {
        window.CPMShopRenderCheckout();
      }

      await loadLoyalty();

      if (showMessage) {
        showToast(
          "Reward removed."
        );
      }

      return true;

    } catch (error) {
      console.warn(
        "[REWARDS] Cancel failed:",
        error
      );

      if (showMessage) {
        showToast(
          error.message ||
            "Unable to remove reward."
        );
      }

      return false;
    }
  }

  /*
   * Selected reward UI
   */

  function renderSelectedReward() {
    const slot =
      $("selectedRewardSlot");

    if (!slot) {
      return;
    }

    if (!selectedRedemption) {
      slot.innerHTML = `
        <div class="rewards-empty">
          No reward selected.
          Redeem points when you are ready
          to checkout.
        </div>
      `;

      return;
    }

    const points =
      Number(
        selectedRedemption.points_spent ||
          0
      );

    const discount =
      Number(
        selectedRedemption.discount_stars ||
          0
      );

    slot.innerHTML = `
      <div class="selected-reward">

        <div>

          <strong>
            ✅ ${points} points reserved
          </strong>

          <span>
            Save ⭐ ${discount}
            at checkout
          </span>

        </div>

        <button
          id="cancelSelectedReward"
          class="text-btn"
          type="button"
        >
          Cancel
        </button>

      </div>
    `;

    const cancelButton =
      $("cancelSelectedReward");

    if (cancelButton) {
      cancelButton.onclick =
        () => {
          cancelSelected(
                        true
          );
        };
    }
  }

  /*
   * Checkout helpers
   */

  function getCheckoutBox() {
    return $("checkoutBox");
  }

  function getSummary() {
    const box =
      getCheckoutBox();

    if (!box) {
      return null;
    }

    return box.querySelector(
      ".summary"
    );
  }

  function getTotalRow() {
    const summary =
      getSummary();

    if (!summary) {
      return null;
    }

    return summary.querySelector(
      ".sum-row.total"
    );
  }

  function extractTotal(
    totalRow
  ) {
    if (!totalRow) {
      return 0;
    }

    const text =
      totalRow.textContent ||
      "";

    const match =
      text.match(
        /([0-9]+)\s*Stars/i
      );

    return Number(
      match?.[1] || 0
    );
  }

  /*
   * Remove reward UI from checkout
   */

  function removeCheckoutReward() {
    const box =
      getCheckoutBox();

    if (!box) {
      return;
    }

    const panel =
      box.querySelector(
        "#checkoutRewardsPanel"
      );

    if (panel) {
      panel.remove();
    }

    const row =
      box.querySelector(
        ".reward-discount-row"
      );

    if (row) {
      row.remove();
    }

    const totalRow =
      box.querySelector(
        ".sum-row.total"
      );

    if (totalRow) {
      delete totalRow.dataset
        .rewardBaseTotal;
    }
  }

  /*
   * Apply reward visually to checkout.
   *
   * This only changes the UI.
   * app.js sends the actual redemption ID.
   */

  function enhanceCheckout() {
    // app.js now owns the complete checkout rendering, including promo,
    // referral and loyalty totals. Keeping this hook side-effect free prevents
    // MutationObserver/render loops when the checkout DOM is rebuilt.
    return;
  }

  /*
   * Watch checkout for app.js
   * rebuilding checkoutBox.
   */

  function watchCheckout() {
    const checkoutView =
      $("checkoutView");

    if (
      !checkoutView ||
      checkoutObserver
    ) {
      return;
    }

    checkoutObserver =
      new MutationObserver(
        () => {
          clearTimeout(
            checkoutObserverTimer
          );

          checkoutObserverTimer =
            setTimeout(
              () => {
                enhanceCheckout();
              },
              80
            );
        }
      );

    checkoutObserver.observe(
      checkoutView,
      {
        childList: true,
        subtree: true
      }
    );
  }

  /*
   * Reward button events
   */

  function attachRewardEvents() {
    document.addEventListener(
      "click",
      (event) => {
        const button =
          event.target.closest(
            "[data-redeem-points]"
          );

        if (!button) {
          return;
        }

        event.preventDefault();
        event.stopPropagation();

        const points =
          Number(
            button.dataset
              .redeemPoints
          );

        if (
          !Number.isFinite(
            points
          ) ||
          points <= 0
        ) {
          return;
        }

        redeem(points);
      },
      false
    );
  }

  /*
   * Refresh checkout after navigation.
   */

  function attachNavigationEvents() {
    document.addEventListener(
      "click",
      (event) => {
        const nav =
          event.target.closest(
            "[data-nav]"
          );

        if (!nav) {
          return;
        }

        setTimeout(
          () => {
            enhanceCheckout();
          },
          150
        );
      },
      false
    );
  }

  /*
   * Public API
   *
   * app.js uses this:
   *
   * window.CPMRewards
   */

  window.CPMRewards = {
    getSelected:
      () =>
        selectedRedemption,

    getSelectedId:
      () =>
        selectedRedemption
          ? Number(
              selectedRedemption.id
            )
          : null,

    getSelectedDiscount:
      () =>
        selectedRedemption
          ? Number(
              selectedRedemption.discount_stars ||
                0
            )
          : 0,

    getSelectedPoints:
      () =>
        selectedRedemption
          ? Number(
              selectedRedemption.points_spent ||
                0
            )
          : 0,

    cancel:
      () =>
        cancelSelected(true),

    refresh:
      () =>
        loadLoyalty(),

    refreshCheckout:
      () =>
        enhanceCheckout()
  };

  /*
   * Initialization
   */

function init() {
  if (initialized) {
    return;
  }

  initialized = true;

  try {
    attachRewardEvents();
  } catch (error) {
    console.error(
      "[REWARDS] Reward events failed:",
      error
    );
  }

  try {
    attachNavigationEvents();
  } catch (error) {
    console.error(
      "[REWARDS] Navigation events failed:",
      error
    );
  }

  try {
    watchCheckout();
  } catch (error) {
    console.error(
      "[REWARDS] Checkout observer failed:",
      error
    );
  }

  /*
   * Load loyalty only once.
   * Never observe loyaltyBox itself here.
   */
  loadLoyalty().catch(
    (error) => {
      console.error(
        "[REWARDS] Initial loyalty load failed:",
        error
      );
    }
  );

  setTimeout(
    () => {
      try {
        enhanceCheckout();
      } catch (error) {
        console.error(
          "[REWARDS] Checkout enhancement failed:",
          error
        );
      }
    },
    500
  );
}

  /*
   * Start safely after DOM.
   */

  if (
    document.readyState ===
    "loading"
  ) {
    document.addEventListener(
      "DOMContentLoaded",
      init,
      {
        once: true
      }
    );
  } else {
    init();
  }

})();
