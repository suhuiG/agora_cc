export function messageForLoginError(error: unknown): string {
  const name =
    error && typeof error === "object" && "name" in error
      ? String((error as { name: unknown }).name)
      : "";

  switch (name) {
    case "NotAuthorizedException":
    case "UserNotFoundException":
      return "이메일 또는 비밀번호가 올바르지 않아요.";
    case "CodeMismatchException":
    case "ExpiredCodeException":
      return "인증 코드가 올바르지 않거나 만료됐어요. 다시 확인해 주세요.";
    case "InvalidPasswordException":
      return "비밀번호가 정책(대·소문자·숫자·기호·12자 이상)을 만족하지 않아요.";
    case "LimitExceededException":
      return "시도가 너무 잦아요. 잠시 후 다시 시도해 주세요.";
    default:
      return "로그인에 실패했어요. 다시 시도해 주세요.";
  }
}
