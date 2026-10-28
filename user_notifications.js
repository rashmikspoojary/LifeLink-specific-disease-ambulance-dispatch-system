(function () {
  const shown = new Set();
  let container = null;

  function ensureContainer() {
    if (container) return container;
    container = document.createElement("div");
    container.id = "lifelink-toast-container";
    container.style.position = "fixed";
    container.style.right = "16px";
    container.style.bottom = "16px";
    container.style.zIndex = "9999";
    container.style.display = "flex";
    container.style.flexDirection = "column";
    container.style.gap = "10px";
    container.style.maxWidth = "360px";
    document.body.appendChild(container);
    return container;
  }

  function markRead(id) {
    return fetch("/api/notifications/mark-read/" + id, {
      method: "POST",
      headers: { "X-Requested-With": "XMLHttpRequest" }
    }).catch(function () {
      return null;
    });
  }

  function createToast(item) {
    const box = document.createElement("div");
    box.style.background = "#0f172a";
    box.style.color = "#ffffff";
    box.style.border = "1px solid #1e293b";
    box.style.borderRadius = "12px";
    box.style.padding = "12px";
    box.style.boxShadow = "0 10px 22px rgba(0,0,0,0.25)";
    box.style.fontFamily = "Segoe UI, Arial, sans-serif";

    const title = document.createElement("div");
    title.textContent = "LifeLink Notification";
    title.style.fontWeight = "700";
    title.style.marginBottom = "6px";

    const msg = document.createElement("div");
    msg.textContent = item.message || "You have a new update.";
    msg.style.fontSize = "14px";
    msg.style.lineHeight = "1.4";

    const time = document.createElement("div");
    time.textContent = item.created_at || "";
    time.style.marginTop = "6px";
    time.style.fontSize = "12px";
    time.style.opacity = "0.8";

    const row = document.createElement("div");
    row.style.marginTop = "10px";
    row.style.display = "flex";
    row.style.gap = "8px";

    const readBtn = document.createElement("button");
    readBtn.type = "button";
    readBtn.textContent = "Mark Read";
    readBtn.style.border = "0";
    readBtn.style.borderRadius = "8px";
    readBtn.style.padding = "6px 10px";
    readBtn.style.fontWeight = "600";
    readBtn.style.cursor = "pointer";
    readBtn.style.background = "#22c55e";
    readBtn.style.color = "#06210f";
    readBtn.onclick = function () {
      markRead(item.id);
      box.remove();
    };

    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.textContent = "Open";
    openBtn.style.borderRadius = "8px";
    openBtn.style.padding = "6px 10px";
    openBtn.style.fontWeight = "600";
    openBtn.style.border = "0";
    openBtn.style.cursor = "pointer";
    openBtn.style.background = "#3b82f6";
    openBtn.style.color = "#ffffff";
    openBtn.onclick = function () {
      window.location.href = "/notifications";
    };

    row.appendChild(readBtn);
    row.appendChild(openBtn);

    box.appendChild(title);
    box.appendChild(msg);
    box.appendChild(time);
    box.appendChild(row);
    ensureContainer().appendChild(box);

    setTimeout(function () {
      if (!document.body.contains(box)) return;
      box.style.opacity = "0";
      box.style.transition = "opacity 0.35s ease";
      setTimeout(function () {
        if (document.body.contains(box)) box.remove();
      }, 350);
      markRead(item.id);
    }, 15000);
  }

  function pollUnread() {
    fetch("/api/notifications/unread", {
      headers: { "X-Requested-With": "XMLHttpRequest" }
    })
      .then(function (res) {
        if (!res.ok) return null;
        return res.json();
      })
      .then(function (json) {
        if (!json || !json.ok || !Array.isArray(json.data)) return;
        json.data.forEach(function (item) {
          if (!item || shown.has(item.id)) return;
          shown.add(item.id);
          createToast(item);
        });
      })
      .catch(function () {
        return null;
      });
  }

  function init() {
    pollUnread();
    setInterval(pollUnread, 10000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
