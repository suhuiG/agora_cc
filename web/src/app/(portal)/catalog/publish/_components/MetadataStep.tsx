import type { FormEventHandler } from "react";
import useSWR from "swr";

import { getCatalogCategories } from "@/lib/api";
import { incrementSemver, SEMVER_RE } from "@/lib/semver";
import { EscalationContactPicker } from "./EscalationContactPicker";
import { inputClass, labelClass } from "./styles";
import {
  type Mode,
  type PublishForm,
  type WizardChoice,
} from "./types";

type MetadataStepProps = {
  choice: WizardChoice;
  mode: Mode;
  form: PublishForm;
  error: string;
  submitting: boolean;
  ownerTeamReadOnly: boolean;
  /** 1차 담당자 — 서버가 principal 에서 파생하는 값. 화면은 표시만 해요 (CA-29). */
  ownerContact: string;
  onUpdateForm: (key: keyof PublishForm, value: string) => void;
  onPrevious: () => void;
  onSubmit: FormEventHandler<HTMLFormElement>;
};

export function MetadataStep({
  choice,
  mode,
  form,
  error,
  submitting,
  ownerTeamReadOnly,
  ownerContact,
  onUpdateForm,
  onPrevious,
  onSubmit,
}: MetadataStepProps) {
  const { data: categoryData } = useSWR(
    "catalog/categories",
    getCatalogCategories,
  );
  const isSkillSource = mode === "source" && choice.assetType === "skill";
  const versionIsValid = SEMVER_RE.test(form.version.trim());
  const categories = categoryData?.items ?? [];
  const currentCategory = form.category.trim();
  const hasUnlistedCurrentCategory = (
    currentCategory !== "" && !categories.includes(currentCategory)
  );

  function bumpVersion(part: "major" | "minor") {
    const next = incrementSemver(form.version, part);
    if (next) onUpdateForm("version", next);
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <p className="text-slate-600 mb-2">
        메타데이터를 입력하고 퍼블리시해 주세요.
      </p>

      <section aria-labelledby="metadata-basic-heading">
        <h2
          id="metadata-basic-heading"
          className="mb-3 text-sm font-semibold text-slate-900"
        >
          기본 정보
        </h2>
        <div className="space-y-4">
          <div>
            <label className={labelClass}>이름 *</label>
            <input
              required
              value={form.name}
              onChange={(event) => onUpdateForm("name", event.target.value)}
              placeholder="my-awesome-skill"
              className={inputClass}
            />
          </div>

          {choice.assetType === "skill" && (
            <p className="text-xs text-slate-400 -mt-2">
              SKILL.md 에서 자동으로 채웠어요. 필요하면 수정할 수 있어요.
            </p>
          )}

          <div>
            <label className={labelClass}>설명</label>
            <textarea
              value={form.description}
              onChange={(event) => onUpdateForm("description", event.target.value)}
              placeholder="이 자산이 무엇을 하는지 간단히 설명해 주세요."
              rows={3}
              className="w-full p-3 border border-slate-300 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div>
            <label className={labelClass}>
              주요 변경사항 {choice.assetType === "skill" ? "(이번 버전)" : ""}
            </label>
            <textarea
              value={form.changelog}
              onChange={(event) => onUpdateForm("changelog", event.target.value)}
              placeholder="이번 버전에서 바뀐 점을 요약해 주세요. (상세 화면 버전 이력에 표시돼요)"
              rows={2}
              className="w-full p-3 border border-slate-300 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div>
            <label className={labelClass}>
              버전 {mode === "source" ? "*" : ""}
            </label>
            {isSkillSource ? (
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                <input
                  aria-label="Skill 버전 직접 입력"
                  value={form.version}
                  onChange={(event) => onUpdateForm("version", event.target.value)}
                  placeholder="1.0.0"
                  className={`${inputClass} min-w-0 sm:flex-1`}
                />
                <div className="flex shrink-0 gap-2">
                  <button
                    type="button"
                    onClick={() => bumpVersion("major")}
                    disabled={!versionIsValid}
                    className="flex-1 px-3 py-2 text-sm whitespace-nowrap border border-slate-300 rounded-lg hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    major +1
                  </button>
                  <button
                    type="button"
                    onClick={() => bumpVersion("minor")}
                    disabled={!versionIsValid}
                    className="flex-1 px-3 py-2 text-sm whitespace-nowrap border border-slate-300 rounded-lg hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    minor +1
                  </button>
                </div>
              </div>
            ) : (
              <input
                value={form.version}
                onChange={(event) => onUpdateForm("version", event.target.value)}
                placeholder="1.0.0"
                className={inputClass}
              />
            )}
            {mode === "source" && !isSkillSource && (
              <p className="text-xs text-slate-400 mt-1">
                semver 형식 · 같은 버전은 덮어쓸 수 없어요.
              </p>
            )}
          </div>
        </div>
      </section>

      <section
        aria-labelledby="metadata-owners-heading"
        className="border-t border-slate-200 pt-5"
      >
        <h2
          id="metadata-owners-heading"
          className="mb-3 text-sm font-semibold text-slate-900"
        >
          담당자
        </h2>
        <div className="space-y-4">
          <div>
            <label className={labelClass}>소유 팀</label>
            <input
              value={form.owner_team}
              onChange={(event) => onUpdateForm("owner_team", event.target.value)}
              readOnly={ownerTeamReadOnly}
              placeholder="silicon2"
              className={`${inputClass} read-only:bg-slate-100 read-only:text-slate-600 read-only:cursor-default`}
            />
          </div>
          {/* 1차 담당자는 **서버가** principal 에서 파생해요 (CA-29 ①). 화면은 읽기
              전용으로 보여주기만 하고 보내지 않아요 — 클라이언트가 이 값을 정하면
              담당자 추적이 위조돼요. */}
          <div>
            <label className={labelClass}>1차 담당자 (등록자)</label>
            <p
              data-testid="owner-contact-readonly"
              className="rounded-lg border border-slate-300 bg-slate-100 px-4 py-2 text-sm text-slate-600"
            >
              {ownerContact || "로그인 계정의 email 을 확인하지 못했어요"}
            </p>
            <p className="mt-1 text-xs text-slate-400">
              {ownerContact
                ? "등록자 계정에서 자동으로 채워요. 변경은 자산 상세의 담당자 정보에서 해요."
                : "email 이 없는 계정이에요 — 승인 전에 관리자가 담당자 연락처를 채워야 해요."}
            </p>
          </div>
          <EscalationContactPicker
            value={form.escalation_contact}
            onChange={(email) => onUpdateForm("escalation_contact", email)}
          />
        </div>
      </section>

      <section
        aria-labelledby="metadata-classification-heading"
        className="border-t border-slate-200 pt-5"
      >
        <h2
          id="metadata-classification-heading"
          className="mb-3 text-sm font-semibold text-slate-900"
        >
          분류
        </h2>
        <div className="space-y-4">
          <div>
            <label className={labelClass}>카테고리</label>
            {categories.length > 0 ? (
              <select
                value={form.category}
                onChange={(event) => onUpdateForm("category", event.target.value)}
                className={inputClass}
              >
                <option value="">선택 안 함</option>
                {hasUnlistedCurrentCategory && (
                  <option value={form.category}>{form.category}</option>
                )}
                {categories.map((category) => (
                  <option key={category} value={category}>
                    {category}
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={form.category}
                onChange={(event) => onUpdateForm("category", event.target.value)}
                placeholder="Operations, CS, ..."
                className={inputClass}
              />
            )}
          </div>
          <div>
            <label className={labelClass}>태그 (쉼표 구분)</label>
            <input
              value={form.tags}
              onChange={(event) => onUpdateForm("tags", event.target.value)}
              placeholder="translation, kbeauty, e-commerce"
              className={inputClass}
            />
          </div>
        </div>
      </section>

      {error && (
        <div className="text-red-600 text-sm bg-red-50 p-3 rounded-lg whitespace-pre-line">
          {error}
        </div>
      )}

      <div className="flex gap-3 pt-2">
        <button
          type="button"
          onClick={onPrevious}
          disabled={submitting}
          className="px-4 py-2 border border-slate-300 rounded-lg hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          이전
        </button>
        <button
          type="submit"
          disabled={submitting || !form.name.trim()}
          className="px-5 py-2 bg-slate-900 text-white rounded-lg hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {mode === "deploy"
            ? submitting
              ? "다음 단계 준비 중..."
              : "다음"
            : submitting
              ? "퍼블리시 중..."
              : "퍼블리시"}
        </button>
      </div>
    </form>
  );
}
