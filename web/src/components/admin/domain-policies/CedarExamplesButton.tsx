"use client";

// 「Cedar 적용 예시 보기」 — 케이스 3가지를 모달로 보여줘요 (제품 오너 요청, 2026-09-06).
//
// 문구·예시는 전부 `@/lib/domainPolicyCopy` 에 있어요. 여기서 리터럴로 쓰면 테스트가 못 읽고,
// 「예시가 이 화면으로 만들 수 있는 형태인지」를 값으로 단정할 수 없게 돼요.
//
// Gateway ARN 은 **서버가 준 값을 그대로 흘려요.** 화면이 좌표를 만들지 않아요.
import { useState } from "react";

import {
  EXAMPLES_ACTION_NOTE,
  EXAMPLES_BUTTON_LABEL,
  EXAMPLES_MODAL_TITLE,
  EXAMPLES_MODE_NOTE,
  EXAMPLES_SCOPE_NOTE,
  domainPolicyExamples,
} from "@/lib/domainPolicyCopy";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import { Modal } from "@/components/ui/modal";

export function CedarExamplesButton({ gatewayArn }: { gatewayArn: string }) {
  const [open, setOpen] = useState(false);
  const examples = domainPolicyExamples(gatewayArn);

  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        <Icon name="eye" size={14} />
        {EXAMPLES_BUTTON_LABEL}
      </Button>

      <Modal
        open={open}
        title={EXAMPLES_MODAL_TITLE}
        description={EXAMPLES_SCOPE_NOTE}
        size="lg"
        onClose={() => setOpen(false)}
        footer={
          <Button variant="outline" onClick={() => setOpen(false)}>
            닫기
          </Button>
        }
      >
        <div className="max-h-[65vh] space-y-4 overflow-y-auto pr-1">
          {examples.map((example, index) => (
            <section
              key={example.id}
              className="rounded-lg border border-border bg-card p-3.5"
            >
              <p className="text-sm font-medium text-foreground">
                케이스 {index + 1} · {example.situation}
              </p>

              <p className="mt-2.5 text-xs font-medium text-muted-foreground">
                이 화면에서 이렇게 만들어요
              </p>
              <ul className="mt-1 space-y-0.5 text-xs">
                {example.form.map((entry) => (
                  <li key={entry.field}>
                    · {entry.field} —{" "}
                    <span className="font-mono">{entry.value}</span>
                  </li>
                ))}
              </ul>

              <p className="mt-2.5 text-xs font-medium text-muted-foreground">
                서버가 만드는 Cedar 문장
              </p>
              <pre className="mt-1 overflow-x-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
                {example.cedar}
              </pre>

              <p className="mt-2 text-xs text-muted-foreground">{example.caution}</p>
            </section>
          ))}

          <ul className="space-y-1 rounded-lg border border-border bg-muted/30 p-3 text-xs text-muted-foreground">
            <li>· {EXAMPLES_ACTION_NOTE}</li>
            <li>· {EXAMPLES_MODE_NOTE}</li>
          </ul>
        </div>
      </Modal>
    </>
  );
}
