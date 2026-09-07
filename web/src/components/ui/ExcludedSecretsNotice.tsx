/**
 * 크리덴셜 파일을 업로드에서 뺐다는 사실을 사용자에게 보여줘요.
 *
 * "비텍스트라 건너뜀" 경고와 **따로** 렌더해요. 두 제외는 이유가 달라서, 섞으면
 * 사용자가 왜 안 올라갔는지 알 수 없어요. 조용히 빼지 않는 게 이 컴포넌트의 존재
 * 이유예요 — 폴더 업로드 화면 다섯 곳이 이걸 공유해요.
 *
 * 서버도 같은 경로를 거부해요(`reject_credential_path` → 422). 여기 표시는 사용자가
 * 업로드를 헛되게 만들지 않도록 미리 알려주는 쪽이에요.
 */
export function ExcludedSecretsNotice({ paths }: { paths: string[] }) {
  if (paths.length === 0) return null;
  return (
    <div className="mt-3 text-xs text-red-700 bg-red-50 border border-red-200 p-3 rounded-lg">
      <span className="font-medium">
        크리덴셜이 들어 있어 업로드하지 않았어요.
      </span>{" "}
      Agent Initializr 로 내려받은 <span className="font-mono">.env</span> 에는
      7일 유효한 dev 크리덴셜이 들어 있어서, 등록하면 소스 저장소에 평문으로 남아요.
      <span className="font-mono"> {paths.join(", ")}</span>
      <div className="mt-1 text-red-600">
        <span className="font-mono">.env.example</span> 은 값이 없어서 그대로
        올라가요. 로컬 실행에는 받은 <span className="font-mono">.env</span> 를
        그대로 쓰시면 돼요.
      </div>
    </div>
  );
}
