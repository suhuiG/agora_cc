// 자산 완전삭제 결과를 «화면 문구» 로 옮기는 판정.
//
// ⚠️ 왜 이 파일이 생겼나 (2026-09-06): 화면이 서버와 **반대되는 말** 을 하고 있었어요.
//
// 서버는 `"registry" not in report.deleted` 일 때 「일부 정리를 완료하지 못해 자산 레코드를
// **보존**했어요」라는 `message` 를 내려줘요(`catalog/router.py` 의 `delete_asset`).
// 그런데 화면은 그 `message` 를 **버리고**(`const { report } = await purgeAsset(...)`) 모달
// 제목을 「삭제 완료 — 일부 리소스 확인 필요」, 설명을 「자산은 **삭제됐지만**…」 으로
// 하드코딩한 뒤 닫으면 `/catalog/browse` 로 보냈어요. 레코드가 살아 있는데 화면은 지웠다고
// 말한 거예요.
//
// 그리고 모달 조건이 `report.failed.length > 0` 이라, **실패는 없는데 레코드는 보존된** 경우
// (정리 클리너가 주입되지 않아 단계가 `skipped` 로만 남는 경로)에는 모달조차 안 뜨고 바로
// 목록으로 이동했어요 — 사용자는 지워진 줄 알아요.
//
// 그래서 판정 기준을 「실패가 있나」에서 **「레코드가 지워졌나」** 로 바꿨어요. 그게 사용자에게
// 유일하게 중요한 사실이에요.
//
// 판정을 `.tsx` 에 두지 않는 이유: 테스트 러너가 `.tsx` 를 실행하지 못해서 음성 대조를 걸 수
// 없어요.

export type PurgeReportShape = {
  deleted: string[];
  failed: { store: string; reason: string }[];
  skipped: string[];
};

/** 레지스트리 레코드 삭제 단계의 라벨. 이 값이 `deleted` 에 있으면 자산이 사라진 거예요. */
export const PURGE_REGISTRY_STAGE = "registry";

export type PurgeOutcome = {
  /** 자산 레코드가 실제로 지워졌나. 사용자에게 유일하게 중요한 사실이에요. */
  recordDeleted: boolean;
  /** 모달을 띄워야 하나. 레코드가 남았거나 실패가 있으면 띄워요. */
  showReport: boolean;
  title: string;
  description: string;
  /** 모달을 닫을 때 목록으로 보낼지. 레코드가 남아 있으면 보내지 않아요. */
  leaveAfterClose: boolean;
};

export const PURGE_PRESERVED_TITLE = "삭제하지 못했어요 — 자산이 남아 있어요";
export const PURGE_PRESERVED_DESCRIPTION =
  "정리하지 못한 항목이 있어서 자산 레코드를 보존했어요. 아래를 확인한 뒤 다시 삭제해 주세요 " +
  "— 앞 단계는 다시 눌러도 안전해요.";

export const PURGE_DELETED_WITH_LEFTOVERS_TITLE = "삭제했어요 — 일부 리소스 확인 필요";
export const PURGE_DELETED_WITH_LEFTOVERS_DESCRIPTION =
  "자산은 삭제됐지만, 아래 리소스는 지금 정리하지 못했어요 (권한·보존 정책 등).";

/**
 * 보고서에서 화면 문구를 도출해요.
 *
 * 기대값의 소유자는 **서버의 `report`** 예요 — 이 함수가 성공을 스스로 판단하지 않아요.
 * `serverMessage` 가 오면 설명으로 그걸 우선 써요(서버가 사유를 더 정확히 알아요).
 */
export function purgeOutcome(
  report: PurgeReportShape,
  serverMessage = "",
): PurgeOutcome {
  const recordDeleted = report.deleted.includes(PURGE_REGISTRY_STAGE);
  const message = serverMessage.trim();

  if (!recordDeleted) {
    return {
      recordDeleted: false,
      // 레코드가 남았으면 실패 목록이 비어 있어도 «반드시» 알려요. 옛 코드가 이 경우를
      // 조용히 넘겨서 사용자가 지워진 줄 알았어요.
      showReport: true,
      title: PURGE_PRESERVED_TITLE,
      description: message || PURGE_PRESERVED_DESCRIPTION,
      leaveAfterClose: false,
    };
  }

  return {
    recordDeleted: true,
    showReport: report.failed.length > 0,
    title: PURGE_DELETED_WITH_LEFTOVERS_TITLE,
    description: message || PURGE_DELETED_WITH_LEFTOVERS_DESCRIPTION,
    leaveAfterClose: true,
  };
}

/**
 * ⚠️ 이 화면이 「완전히 삭제했어요」라고 단정하면 안 되는 이유.
 *
 * 2026-09-06 조사에서 purge 가 «닿지 않는» 저장소가 여러 개 확인됐어요 — 버전 버킷의 소스
 * 바이트(삭제 마커만 붙어요), 도구 드리프트 원장, 담당자 변경 이력, CloudWatch 로그그룹,
 * ECR 이미지 등. 그래서 성공 토스트도 「지웠어요」까지만 말하고 «완전» 을 붙이지 않아요.
 * 목록은 `docs/06-risks.md` 에 있어요.
 */
export const PURGE_SUCCESS_TITLE = "삭제했어요.";
export function purgeSuccessDetail(assetName: string): string {
  return `${assetName} 의 모든 버전과 카탈로그 데이터를 지웠어요.`;
}
