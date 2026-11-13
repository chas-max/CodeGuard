/** 极简 hash 路由：避免额外依赖，且静态托管在 /ui 下无需服务端改写。 */
import { useEffect, useState } from "react";

export type Route =
  | { name: "dashboard" }
  | { name: "jobs" }
  | { name: "job"; id: string }
  | { name: "settings" }
  | { name: "watchers" };

function parse(raw: string): Route {
  // 回调会带 `?github=connected`，路由只看 query 之前的路径。
  const [pathname] = raw.split("?");
  const path = pathname.replace(/^#/, "").replace(/^\//, "");
  const [head, second] = path.split("/");
  if (head === "jobs" && second) return { name: "job", id: second };
  if (head === "jobs") return { name: "jobs" };
  if (head === "settings") return { name: "settings" };
  if (head === "watchers") return { name: "watchers" };
  return { name: "dashboard" };
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parse(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export function navigate(path: string): void {
  window.location.hash = path.startsWith("/") ? path : `/${path}`;
}

export function href(path: string): string {
  return `#${path.startsWith("/") ? path : `/${path}`}`;
}
