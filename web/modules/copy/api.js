/** 本机跟单记录、规则与未知结果核对接口。 */
import { fetchJson, postJson } from "../http.js";

export async function readFollowRecords({ filter: status, search, limit, offset }) {
  const query = new URLSearchParams({ status, search, limit: String(limit), offset: String(offset) });
  const { response, payload } = await fetchJson(`/api/follow?${query}`);
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `读取跟单记录失败（${response.status}）`);
  }
  return payload;
}

export async function readFollowDetail(id) {
  const { response, payload } = await fetchJson(`/api/follow/groups/${encodeURIComponent(id)}`);
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `读取明细失败（${response.status}）`);
  }
  return payload;
}

export const saveFollowRule = (rule) => postJson("/api/follow/rule", rule);
export const pauseFollow = () => postJson("/api/follow/pause");
