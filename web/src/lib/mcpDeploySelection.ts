export function buildMcpDeployStartBody(
  assetId: string,
  version: string,
  selectedTools: string[],
) {
  return {
    asset_id: assetId,
    version,
    selected_tools: selectedTools,
  };
}
