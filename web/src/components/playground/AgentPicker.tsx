"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import {
  listDeployedAgents,
  type DeployedAgent,
} from "@/lib/api";
import { requestGenerationMatches } from "@/lib/playground/runConfig";

const RECENT_KEY = "agora.playground.recent-agents";
const RECENT_LIMIT = 5;

function readRecent(): DeployedAgent[] {
  try {
    const value = JSON.parse(window.localStorage.getItem(RECENT_KEY) || "[]");
    return Array.isArray(value)
      ? value.filter((item) =>
        item
        && typeof item.record_id === "string"
        && typeof item.name === "string"
        && typeof item.version === "string")
      : [];
  } catch {
    return [];
  }
}

function remember(agent: DeployedAgent): DeployedAgent[] {
  const next = [
    agent,
    ...readRecent().filter((item) => item.record_id !== agent.record_id),
  ].slice(0, RECENT_LIMIT);
  try {
    // 최근 목록은 이 브라우저의 선택 이력만 저장하며 서버/다른 기기와 공유하지 않아요.
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    // 저장 실패는 선택 자체를 막지 않아요.
  }
  return next;
}

export function AgentPicker({
  selected,
  disabled,
  onSelect,
}: {
  selected: DeployedAgent | null;
  disabled: boolean;
  onSelect: (agent: DeployedAgent) => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<DeployedAgent[]>([]);
  const [recent, setRecent] = useState<DeployedAgent[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [incompleteReason, setIncompleteReason] = useState("");
  const requestId = useRef(0);
  const activeQueryRef = useRef("");

  const runSearch = useCallback((nextQuery: string) => {
    const id = ++requestId.current;
    activeQueryRef.current = nextQuery;
    setQuery(nextQuery);
    setLoading(true);
    setError("");
    void listDeployedAgents({ name: nextQuery }).then((page) => {
      if (id !== requestId.current) return;
      setItems(page.items);
      setNextCursor(page.next_cursor);
      setIncompleteReason(page.incomplete_reason);
    }).catch((caught) => {
      if (id !== requestId.current) return;
      setItems([]);
      setNextCursor(null);
      setIncompleteReason("");
      setError(caught instanceof Error ? caught.message : "agent 목록을 읽지 못했어요.");
    }).finally(() => {
      if (id === requestId.current) setLoading(false);
    });
  }, []);

  useEffect(() => {
    if (!open) return;
    const timeout = window.setTimeout(() => runSearch(draft.trim()), 250);
    return () => window.clearTimeout(timeout);
  }, [draft, open, runSearch]);

  function openPicker() {
    requestId.current += 1;
    activeQueryRef.current = "";
    setRecent(readRecent());
    setOpen(true);
    setDraft("");
    setItems([]);
    setNextCursor(null);
    setError("");
    setIncompleteReason("");
    setLoading(true);
  }

  async function loadMore() {
    if (!nextCursor || loading) return;
    const request = {
      recordId: query,
      generation: ++requestId.current,
    };
    const cursor = nextCursor;
    setLoading(true);
    setError("");
    try {
      const page = await listDeployedAgents({
        name: query,
        cursor,
      });
      if (!requestGenerationMatches(
        { recordId: activeQueryRef.current, generation: requestId.current },
        request,
      )) return;
      setItems((current) => [...current, ...page.items]);
      setNextCursor(page.next_cursor);
      if (page.incomplete_reason) {
        setIncompleteReason(page.incomplete_reason);
      }
    } catch (caught) {
      if (!requestGenerationMatches(
        { recordId: activeQueryRef.current, generation: requestId.current },
        request,
      )) return;
      setError(caught instanceof Error ? caught.message : "다음 페이지를 읽지 못했어요.");
    } finally {
      if (requestGenerationMatches(
        { recordId: activeQueryRef.current, generation: requestId.current },
        request,
      )) setLoading(false);
    }
  }

  function changeDraft(nextDraft: string) {
    requestId.current += 1;
    const nextQuery = nextDraft.trim();
    activeQueryRef.current = nextQuery;
    setDraft(nextDraft);
    setQuery(nextQuery);
    setItems([]);
    setNextCursor(null);
    setError("");
    setIncompleteReason("");
    setLoading(true);
  }

  function closePicker() {
    requestId.current += 1;
    setOpen(false);
  }

  function choose(agent: DeployedAgent) {
    setRecent(remember(agent));
    onSelect(agent);
    closePicker();
  }

  const recentRows = query ? [] : recent;
  const visibleItems = query
    ? items
    : items.filter((item) =>
      !recentRows.some((recentAgent) => recentAgent.record_id === item.record_id));

  return (
    <>
      <button
        type="button"
        disabled={disabled}
        onClick={openPicker}
        className="flex w-full max-w-md items-center justify-between rounded-lg border border-border bg-card px-3 py-2 text-left text-sm disabled:cursor-not-allowed disabled:opacity-60"
      >
        <span className={selected ? "text-foreground" : "text-muted-foreground"}>
          {selected ? `${selected.name} (v${selected.version})` : "agent를 선택하세요"}
        </span>
        <Icon name="search" size={15} className="text-muted-foreground" />
      </button>
      <Modal
        open={open}
        title="agent 선택"
        description="배포 완료된 agent를 이름으로 검색해요."
        onClose={closePicker}
        size="lg"
      >
        <div className="space-y-3">
          <div className="relative">
            <Icon
              name="search"
              size={15}
              className="pointer-events-none absolute left-3 top-2.5 text-muted-foreground"
            />
            <Input
              value={draft}
              onChange={(event) => changeDraft(event.target.value)}
              placeholder="agent 이름 검색"
              className="pl-9"
            />
          </div>
          <div className="max-h-80 overflow-y-auto border-y border-border">
            {recentRows.length > 0 && (
              <div className="border-b border-border py-2">
                <p className="px-2 pb-1 text-[11px] font-medium text-muted-foreground">
                  최근 사용 · 이 브라우저 기록, 현재 배포 여부 미확인
                </p>
                {recentRows.map((agent) => (
                  <AgentRow key={`recent-${agent.record_id}`} agent={agent} onChoose={choose} />
                ))}
              </div>
            )}
            {visibleItems.map((agent) => (
              <AgentRow key={agent.record_id} agent={agent} onChoose={choose} />
            ))}
            {loading && items.length === 0 && (
              <p className="px-2 py-8 text-center text-sm text-muted-foreground">
                검색 중이에요...
              </p>
            )}
            {!loading && error && (
              <p className="px-2 py-8 text-center text-sm text-red-700">
                목록 조회 실패: {error}
              </p>
            )}
            {!loading && !error && incompleteReason && (
              <p className="px-2 py-3 text-center text-sm text-amber-700">
                목록 미관측: {incompleteReason}
              </p>
            )}
            {!loading && !error && !incompleteReason && items.length === 0 && (
              <p className="px-2 py-8 text-center text-sm text-muted-foreground">
                {nextCursor
                  ? "현재 검색 구간에는 결과가 없어요. 더 보기를 눌러 계속 찾아보세요."
                  : query
                  ? "이 이름과 일치하는 검색 결과가 없어요."
                  : "배포된 agent가 없어요. Agent Initializr에서 먼저 배포해 주세요."}
              </p>
            )}
          </div>
          {nextCursor && (
            <Button
              variant="outline"
              size="sm"
              disabled={loading}
              onClick={() => void loadMore()}
              className="w-full"
            >
              더 보기
            </Button>
          )}
        </div>
      </Modal>
    </>
  );
}

function AgentRow({
  agent,
  onChoose,
}: {
  agent: DeployedAgent;
  onChoose: (agent: DeployedAgent) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onChoose(agent)}
      className="flex w-full items-center justify-between px-2 py-2 text-left hover:bg-accent"
    >
      <span className="min-w-0 truncate text-sm font-medium">{agent.name}</span>
      <span className="ml-3 shrink-0 font-mono text-xs text-muted-foreground">
        v{agent.version}
      </span>
    </button>
  );
}
