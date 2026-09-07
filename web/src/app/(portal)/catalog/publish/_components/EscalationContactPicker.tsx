"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";

import { searchDirectoryMembers } from "@/lib/api";
import { inputClass, labelClass } from "./styles";

/**
 * 2차 담당자(에스컬레이션) 선택 — 회원 검색만 (CA-29 ②).
 *
 * 자유 입력을 없앤 이유: 예전 폼은 아무 문자열이나 받았고(`admin`·온콜 채널 이름) 승인
 * 계약은 **유효한 email 2개**를 요구해서, 등록은 성공하고 승인만 조용히 막혔어요(CA-28).
 *
 * 등록자 본인은 서버가 검색 결과에서 제외해요 — 계약의
 * `distinct_escalation_contact_required` 와 같은 규칙이에요.
 */
type Props = {
  /** 선택된 회원의 email. 빈 문자열이면 아직 안 골랐어요. */
  value: string;
  onChange: (email: string) => void;
  /**
   * 아직 아무도 안 고른 상태의 첫 모습.
   *
   * `"input"`(기본)은 검색 입력란을 바로 펼쳐요 — 등록 폼은 이 필드가 필수 항목 중
   * 하나라 바로 보이는 편이 나아요. `"button"`은 `검색` 버튼만 두고 누를 때 펼쳐요 —
   * Initializr 처럼 설정이 많은 화면에서 세로 길이를 아끼려고요.
   */
  initialMode?: "input" | "button";
};

const MIN_QUERY = 2;

export function EscalationContactPicker({
  value,
  onChange,
  initialMode = "input",
}: Props) {
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [searchOpen, setSearchOpen] = useState(initialMode === "input");

  useEffect(() => {
    // setState 는 타이머 콜백 안에서만 불러요(effect 본문 동기 호출 금지 규칙).
    const timer = setTimeout(() => setDebounced(query.trim()), 250);
    return () => clearTimeout(timer);
  }, [query]);

  const needle = debounced.length >= MIN_QUERY ? debounced : null;
  const { data, error, isLoading } = useSWR(
    needle ? ["directory/members", needle] : null,
    ([, q]: [string, string]) => searchDirectoryMembers(q),
    { revalidateOnFocus: false, keepPreviousData: false },
  );

  const members = data?.items ?? [];
  // 조회 실패와 "결과 없음" 을 구분해요 — 실패를 빈 목록으로 접으면 사용자가 그 사람이
  // 없다고 오해해요(ADR-0037 §4 의 같은 규율).
  const message = error
    ? `회원을 검색하지 못했어요: ${
        error instanceof Error ? error.message : "알 수 없는 오류"
      }`
    : needle && !isLoading && members.length === 0
      ? "검색 결과가 없어요."
      : "";

  return (
    <div>
      <label className={labelClass} htmlFor="escalation-contact-search">
        2차 담당자(에스컬레이션) *
      </label>
      {value ? (
        <div className="flex items-center gap-2">
          <span
            data-testid="escalation-selected"
            className="flex-1 truncate rounded-lg border border-slate-300 bg-slate-50 px-4 py-2 text-sm text-slate-700"
          >
            {value}
          </span>
          <button
            type="button"
            onClick={() => {
              onChange("");
              setQuery("");
              setSearchOpen(true);
            }}
            className="shrink-0 rounded-lg border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50"
          >
            변경
          </button>
        </div>
      ) : !searchOpen ? (
        <button
          type="button"
          data-testid="escalation-search-open"
          onClick={() => setSearchOpen(true)}
          className="w-full rounded-lg border border-slate-300 px-4 py-2 text-left text-sm text-slate-600 hover:bg-slate-50"
        >
          검색
        </button>
      ) : (
        <>
          <input
            id="escalation-contact-search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="이름 또는 email 로 회원 검색 (2자 이상)"
            className={inputClass}
            autoComplete="off"
          />
          {needle && isLoading && (
            <p className="mt-1 text-xs text-slate-400">검색 중…</p>
          )}
          {message && (
            <p
              className={`mt-1 text-xs ${
                error ? "text-red-600" : "text-slate-500"
              }`}
            >
              {message}
            </p>
          )}
          {members.length > 0 && (
            <ul className="mt-1 max-h-48 divide-y divide-slate-100 overflow-auto rounded-lg border border-slate-200">
              {members.map((member) => (
                <li key={member.email}>
                  <button
                    type="button"
                    onClick={() => onChange(member.email)}
                    className="flex w-full flex-col items-start px-4 py-2 text-left hover:bg-slate-50"
                  >
                    <span className="text-sm text-slate-800">
                      {member.name || member.email}
                    </span>
                    <span className="text-xs text-slate-500">{member.email}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {data?.truncated && (
            <p className="mt-1 text-xs text-amber-700">
              결과가 많아 10명까지만 보여줘요 — 검색어를 더 좁혀 주세요.
            </p>
          )}
        </>
      )}
      <p className="mt-1 text-xs text-slate-400">
        소유자가 부재일 때 연락할 다른 구성원이에요. 본인은 고를 수 없어요.
      </p>
    </div>
  );
}
