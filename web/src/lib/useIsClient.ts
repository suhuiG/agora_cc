import { useSyncExternalStore } from "react";

// SSR 가드 — 서버 렌더에서는 false, 클라이언트 마운트 후 true.
//
// `createPortal(…, document.body)` 를 쓰는 컴포넌트(모달·토스트)는 document 가 있는지
// 확인해야 해요. `useEffect(() => setMounted(true))` 로 하면 react-hooks/set-state-in-effect
// 가 막아서(불필요한 리렌더 유발), 구독 형태로 같은 결과를 얻어요.
//
// 구독 함수가 아무것도 안 하는 건 의도예요 — 값이 절대 바뀌지 않으니(서버→클라이언트 전환은
// hydration 한 번뿐) 변경 통지가 필요 없어요.
const NOOP_SUBSCRIBE = () => () => {};
const CLIENT_SNAPSHOT = () => true;
const SERVER_SNAPSHOT = () => false;

export function useIsClient(): boolean {
  return useSyncExternalStore(NOOP_SUBSCRIBE, CLIENT_SNAPSHOT, SERVER_SNAPSHOT);
}
