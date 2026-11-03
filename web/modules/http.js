/** JSON fetch with a hard timeout; the server is always same-origin. */
export async function fetchJson(url, init = {}, { timeoutMs = 8000, allowInvalidJson = false } = {}) {
  const controller = new AbortController();
  const signal = init.signal ? AbortSignal.any([init.signal, controller.signal]) : controller.signal;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...init, signal });
    let payload;
    try {
      payload = await response.json();
    } catch (error) {
      if (!allowInvalidJson || error.name === "AbortError") throw error;
      payload = {};
    }
    return { response, payload };
  } finally {
    clearTimeout(timer);
  }
}

export async function postJson(url, body = {}) {
  const { response, payload } = await fetchJson(
    url,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    },
    { timeoutMs: 15000, allowInvalidJson: true },
  );
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}
