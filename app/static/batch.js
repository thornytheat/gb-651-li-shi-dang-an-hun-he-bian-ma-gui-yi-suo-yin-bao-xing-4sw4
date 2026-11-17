const out = document.querySelector("#batch-output");
const show = (value) => { out.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2); };

async function call(url, options = {}) {
  const response = await fetch(url, {credentials: "same-origin", ...options});
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = (body && body.message) || response.statusText || "请求失败";
    show({error: (body && body.error) || response.status, message});
    return null;
  }
  show(body);
  return body;
}

document.querySelector("#grant-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const ids = [...event.target.querySelector("[name=archive_ids]").selectedOptions].map(o => o.value);
  call(`/api/grants/${encodeURIComponent(form.get("library_id"))}`, {
    method: "PUT", headers: {"content-type": "application/json"},
    body: JSON.stringify({archive_ids: ids, note: "批次页授权"}),
  }).then(refresh);
});

document.querySelector("#revoke-grant")?.addEventListener("click", () => {
  const library = document.querySelector("#grant-form [name=library_id]").value;
  call(`/api/grants/${encodeURIComponent(library)}/revoke`, {
    method: "POST", headers: {"content-type": "application/json"}, body: "{}",
  }).then(refresh);
});

document.querySelector("#delivery-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  call("/api/deliveries", {
    method: "POST", headers: {"content-type": "application/json"},
    body: JSON.stringify({idempotency_key: form.get("idempotency_key")}),
  }).then(refresh);
});

document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    const id = button.dataset.id;
    switch (button.dataset.action) {
      case "process":
        await call(`/api/deliveries/${encodeURIComponent(id)}/process`, {
          method: "POST", headers: {"content-type": "application/json"}, body: "{}",
        });
        break;
      case "interrupt":
        await call(`/api/deliveries/${encodeURIComponent(id)}/process`, {
          method: "POST", headers: {"content-type": "application/json"},
          body: JSON.stringify({interrupt_after: 1}),
        });
        break;
      case "ticket":
        await call(`/api/deliveries/${encodeURIComponent(id)}/tickets`, {
          method: "POST", headers: {"content-type": "application/json"}, body: "{}",
        });
        break;
      case "download": {
        const response = await fetch(`/api/tickets/${encodeURIComponent(id)}/download`, {
          method: "POST", credentials: "same-origin",
        });
        if (!response.ok) { show(await response.json().catch(() => ({}))); break; }
        const blob = await response.blob();
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `${id}.csv`;
        link.click();
        URL.revokeObjectURL(link.href);
        show(`已下载 · 清单摘要 ${response.headers.get("x-manifest-digest")}`);
        refresh();
        break;
      }
      case "reproduce":
        await call(`/api/deliveries/${encodeURIComponent(id)}/reproduce`);
        break;
    }
  });
});

const refresh = () => setTimeout(() => location.reload(), 400);
