"use client";

import { createPortal } from "react-dom";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/ui";
import { type FileNode } from "@/lib/initializr";

// 트리를 깊이우선으로 순회해 모든 파일 노드를 평탄화해요(기본 선택 탐색용).
function flattenFiles(nodes: FileNode[]): FileNode[] {
  const out: FileNode[] = [];
  for (const n of nodes) {
    if (n.kind === "file") out.push(n);
    if (n.children) out.push(...flattenFiles(n.children));
  }
  return out;
}

// Explore 화면 — 좌: 파일트리, 우: 코드뷰(라인번호). Spring Initializr의 EXPLORE 대응.
// files prop으로 스캐폴드 파일 트리를 받아요. DOWNLOAD는 onDownload 콜백으로 연동돼요.
export function ExploreModal({
  files: fileTree, error, scaffoldName, onDownload, onClose,
}: {
  files: FileNode[];
  error?: string;
  scaffoldName: string;
  onDownload: () => void;
  onClose: () => void;
}) {
  const files = flattenFiles(fileTree);
  const [selected, setSelected] = useState<FileNode | undefined>(
    files.find((f) => f.path === "agent/main.py") ?? files[0],
  );
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onEsc);
    return () => document.removeEventListener("keydown", onEsc);
  }, [onClose]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(selected?.content ?? "");
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* 클립보드 권한 없으면 무시 */ }
  };

  const lines = (selected?.content ?? "").replace(/\n$/, "").split("\n");

  // 파일이 없으면 로딩 또는 API 오류를 구분해 보여줘요.
  if (files.length === 0) {
    return createPortal(
      <div
        className="fixed inset-0 z-[var(--z-modal-backdrop)] flex items-center justify-center bg-black/40 p-4"
        onClick={onClose}
      >
        <div
          role="dialog"
          aria-modal="true"
          aria-label="생성될 프로젝트 미리보기"
          className="flex h-[85vh] w-full max-w-5xl flex-col items-center justify-center overflow-hidden rounded-xl border border-border bg-card shadow-xl z-[var(--z-modal)]"
          onClick={(e) => e.stopPropagation()}
        >
          <p className={cn(
            "max-w-lg px-6 text-center text-sm",
            error ? "text-red-700" : "text-muted-foreground",
          )}>
            {error || "구성을 불러오는 중이에요…"}
          </p>
          {error && (
            <Button className="mt-4" variant="outline" onClick={onClose}>
              닫기
            </Button>
          )}
        </div>
      </div>,
      document.body,
    );
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[var(--z-modal-backdrop)] flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="생성될 프로젝트 미리보기"
        className="flex h-[85vh] w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-border bg-card shadow-xl z-[var(--z-modal)]"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 헤더 */}
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div className="flex items-center gap-2">
            <FolderZipIcon />
            <span className="font-mono text-sm font-semibold">{scaffoldName}.zip</span>
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="outline" onClick={copy}>
              {copied ? "복사됨" : "COPY"}
            </Button>
            <Button size="sm" variant="primary" onClick={onDownload}>
              DOWNLOAD
            </Button>
            <button type="button" onClick={onClose}
              className="ml-1 rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground">✕</button>
          </div>
        </div>

        {/* 본문: 트리 + 코드 */}
        <div className="flex min-h-0 flex-1">
          {/* 파일트리 (agent/ 폴더는 펼침/접힘) */}
          <aside className="w-64 shrink-0 overflow-auto border-r border-border py-2">
            <ul>
              {fileTree.map((node) => (
                <TreeNode key={node.path} node={node} depth={0} selected={selected} onSelect={setSelected} />
              ))}
            </ul>
          </aside>

          {/* 코드뷰 */}
          <div className="min-w-0 flex-1 overflow-auto bg-slate-50">
            <div className="flex font-mono text-[12.5px] leading-6">
              {/* 라인번호 */}
              <div className="select-none border-r border-border bg-slate-100 px-3 py-3 text-right text-slate-400">
                {lines.map((_, i) => <div key={i}>{i + 1}</div>)}
              </div>
              {/* 코드 */}
              <pre className="flex-1 overflow-x-auto px-4 py-3 text-slate-800">
                {lines.map((ln, i) => <div key={i}>{ln === "" ? " " : ln}</div>)}
              </pre>
            </div>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

// 재귀 트리 노드 — 폴더는 펼침/접힘 토글, 파일은 선택. depth로 들여쓰기.
function TreeNode({
  node, depth, selected, onSelect,
}: { node: FileNode; depth: number; selected: FileNode | undefined; onSelect: (n: FileNode) => void }) {
  // 폴더는 기본 펼침(스캐폴드가 얕아 한눈에 보이게).
  const [open, setOpen] = useState(true);
  const pad = { paddingLeft: `${depth * 14 + 16}px` };

  if (node.kind === "dir") {
    return (
      <li>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          style={pad}
          className="flex w-full items-center gap-2 py-1.5 pr-4 text-left text-sm text-foreground hover:bg-accent/60"
        >
          <ChevronIcon open={open} />
          <FolderIcon />
          <span className="truncate font-medium">{node.name}</span>
        </button>
        {open && node.children && (
          <ul>
            {node.children.map((c) => (
              <TreeNode key={c.path} node={c} depth={depth + 1} selected={selected} onSelect={onSelect} />
            ))}
          </ul>
        )}
      </li>
    );
  }

  const active = node.path === selected?.path;
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(node)}
        style={pad}
        className={cn(
          "flex w-full items-center gap-2 py-1.5 pr-4 text-left text-sm",
          active ? "bg-accent font-medium text-blue-700" : "text-foreground hover:bg-accent/60",
        )}
      >
        <span className="w-3 shrink-0" aria-hidden />
        <FileIcon />
        <span className="truncate">{node.name}</span>
      </button>
    </li>
  );
}

// ── 인라인 아이콘 (외부 라이브러리 없음, icon.tsx 관례) ──────────────
function FileIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0 text-slate-400" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" />
    </svg>
  );
}
function FolderIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0 text-slate-500" fill="currentColor">
      <path d="M10 4H2v16h20V6H12l-2-2z" />
    </svg>
  );
}
function ChevronIcon({ open }: { open?: boolean }) {
  return (
    <svg viewBox="0 0 24 24"
      className={cn("h-3 w-3 shrink-0 text-slate-400 transition-transform", open && "rotate-90")}
      fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}
function FolderZipIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5 text-slate-700" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M4 4h6l2 2h8v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z" />
      <path d="M14 10v2m0 2v2" />
    </svg>
  );
}
