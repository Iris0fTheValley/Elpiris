import createClient from 'openapi-fetch';

import type {paths} from './generated';
import type {
  AcquireSourceMediaRequest,
  BgmTrack,
  ChangeMediaLifecycleRequest,
  CreateStoryRequest,
  CreateVideoBatch,
  FirstReviewSubmission,
  HealthReport,
  OperationRun,
  ProblemDetail,
  RenderVideoBatch,
  RetranslateStoryRequest,
  RetentionCleanupCommand,
  ReviewSourceTranscriptionRequest,
  RoleProfileCreate,
  RoleProfileReplace,
  ScheduleSnapshot,
  SetVideoCapabilityPolicy,
  ScriptReviewSubmission,
  SecondReviewSubmission,
  StageCommonsVisualRequest,
  SourceRunRequest,
  StartSourceRunRequest,
  SourceRunStatus,
  StartSourceTranscriptionRequest,
  StoryStatus,
  StoryVisualAssets,
  StoryUpdate,
  SynthesizeBatchNarration,
  SynthesizeStoryRequest,
  SubmitNarrationReview,
  SubmitTimelineReview,
  VideoBatchStatus,
  VisualAssetMutation,
  VisualDiscoveryReviewRequest,
  ReuseApprovedVisualRequest,
} from './types';

export interface SourceRunListParams {
  source?: SourceRunRequest['source'] | null;
  run_status?: SourceRunStatus | null;
  limit?: number;
  offset?: number;
}

export interface VideoBatchListParams {
  status?: VideoBatchStatus | null;
  limit?: number;
  offset?: number;
}

export interface MediaCatalogListParams {
  search?: string | null;
  source_kind?: 'visual_asset' | 'visual_discovery' | 'source_media' | null;
  media_kind?: 'image' | 'video' | null;
  lifecycle?: 'active' | 'archived' | null;
  story_id?: string | null;
  publish_eligible?: boolean | null;
  limit?: number;
  offset?: number;
}

export type RuntimeAction = 'restart' | 'shutdown';

const api = createClient<paths>({baseUrl: ''});

export class ApiProblem extends Error {
  readonly code: string;
  readonly status: number;
  readonly storyId: string | null;

  constructor(problem: ProblemDetail, status: number) {
    super(problem.message);
    this.name = 'ApiProblem';
    this.code = problem.code;
    this.status = status;
    this.storyId = problem.story_id ?? null;
  }
}

export async function getReadiness(): Promise<HealthReport> {
  const result = await api.GET('/api/v1/health/ready');
  if (result.data !== undefined) return result.data;
  if (
    typeof result.error === 'object'
    && result.error !== null
    && 'ready' in result.error
    && 'checks' in result.error
  ) {
    return result.error;
  }
  throwProblem(result.error, result.response);
}

export async function getRuntimeControlStatus() {
  const result = await api.GET('/api/v1/system/runtime');
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function requestRuntimeAction(action: RuntimeAction) {
  const result = await api.POST('/api/v1/system/runtime/{action}', {
    params: {path: {action}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function listVideoTemplates() {
  const result = await api.GET('/api/v1/video/templates');
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function getVideoCapabilityRegistry() {
  const result = await api.GET('/api/v1/video/registry');
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function setVideoCapabilityPolicy(body: SetVideoCapabilityPolicy) {
  const result = await api.PUT('/api/v1/video/registry/policy', {body});
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

function throwProblem(error: unknown, response: Response): never {
  const fallback: ProblemDetail = {
    code: 'unexpected_response',
    message: `服务返回了无法识别的错误（HTTP ${String(response.status)}）。`,
    trace_id: response.headers.get('X-Trace-ID') ?? 'unknown',
    story_id: null,
  };
  if (typeof error === 'object' && error !== null && 'code' in error && 'message' in error) {
    throw new ApiProblem(error as ProblemDetail, response.status);
  }
  throw new ApiProblem(fallback, response.status);
}

export async function listStories(status?: StoryStatus) {
  const result = await api.GET('/api/v1/stories', {
    params: {query: {status, limit: 50, offset: 0}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function getStory(storyId: string) {
  const result = await api.GET('/api/v1/stories/{story_id}', {
    params: {path: {story_id: storyId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function createStory(body: CreateStoryRequest) {
  const result = await api.POST('/api/v1/stories', {body});
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function submitFirstReview(storyId: string, body: FirstReviewSubmission) {
  const result = await api.POST('/api/v1/stories/{story_id}/reviews/first', {
    params: {path: {story_id: storyId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function submitSecondReview(storyId: string, body: SecondReviewSubmission) {
  const result = await api.POST('/api/v1/stories/{story_id}/reviews/second', {
    params: {path: {story_id: storyId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function submitScriptReview(storyId: string, body: ScriptReviewSubmission) {
  const result = await api.POST('/api/v1/stories/{story_id}/reviews/script', {
    params: {path: {story_id: storyId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function synthesizeStory(storyId: string, body: SynthesizeStoryRequest) {
  const result = await api.POST('/api/v1/stories/{story_id}/synthesize', {
    params: {path: {story_id: storyId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function resumeStory(storyId: string) {
  const result = await api.POST('/api/v1/stories/{story_id}/resume', {
    params: {path: {story_id: storyId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function retranslateStory(storyId: string, body: RetranslateStoryRequest) {
  const result = await api.POST('/api/v1/stories/{story_id}/retranslate', {
    params: {path: {story_id: storyId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function listReviews(storyId: string) {
  const result = await api.GET('/api/v1/stories/{story_id}/reviews', {
    params: {path: {story_id: storyId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function listTransitions(storyId: string) {
  const result = await api.GET('/api/v1/stories/{story_id}/transitions', {
    params: {path: {story_id: storyId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function getProductionManifest(storyId: string) {
  const result = await api.GET('/api/v1/stories/{story_id}/production-manifest', {
    params: {path: {story_id: storyId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function getClassificationMetrics() {
  const result = await api.GET('/api/v1/metrics/classification');
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function getSourceHealth(probeNetwork = false) {
  const result = await api.GET('/api/v1/sources/health', {
    params: {query: {probe_network: probeNetwork}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export function audioClipUrl(storyId: string, segmentId: string): string {
  return `/api/v1/stories/${encodeURIComponent(storyId)}/audio/${encodeURIComponent(segmentId)}`;
}

/* ── Immutable source-media evidence ── */

export async function listSourceMediaArtifacts(storyId: string) {
  const result = await api.GET('/api/v1/stories/{story_id}/source-media', {
    params: {path: {story_id: storyId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function acquireSourceMedia(storyId: string, body: AcquireSourceMediaRequest) {
  const result = await api.POST('/api/v1/stories/{story_id}/source-media/acquire', {
    params: {path: {story_id: storyId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

/** Scoped browser URL; immutable host storage paths never cross the API boundary. */
export function sourceMediaContentUrl(storyId: string, artifactId: string): string {
  return `/api/v1/stories/${encodeURIComponent(storyId)}/source-media/${encodeURIComponent(artifactId)}/content`;
}

export async function listSourceMediaTranscriptions(storyId: string, artifactId: string) {
  const result = await api.GET(
    '/api/v1/stories/{story_id}/source-media/{artifact_id}/transcriptions',
    {params: {path: {story_id: storyId, artifact_id: artifactId}}},
  );
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function startSourceMediaTranscription(
  storyId: string,
  artifactId: string,
  body: StartSourceTranscriptionRequest,
) {
  const result = await api.POST(
    '/api/v1/stories/{story_id}/source-media/{artifact_id}/transcriptions',
    {params: {path: {story_id: storyId, artifact_id: artifactId}}, body},
  );
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function reviewSourceMediaTranscription(
  storyId: string,
  artifactId: string,
  transcriptionId: string,
  body: ReviewSourceTranscriptionRequest,
) {
  const result = await api.POST(
    '/api/v1/stories/{story_id}/source-media/{artifact_id}/transcriptions/{transcription_id}/review',
    {
      params: {
        path: {
          story_id: storyId,
          artifact_id: artifactId,
          transcription_id: transcriptionId,
        },
      },
      body,
    },
  );
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function cancelSourceMediaTranscription(
  storyId: string,
  artifactId: string,
  transcriptionId: string,
) {
  const result = await api.POST(
    '/api/v1/stories/{story_id}/source-media/{artifact_id}/transcriptions/{transcription_id}/cancel',
    {
      params: {
        path: {
          story_id: storyId,
          artifact_id: artifactId,
          transcription_id: transcriptionId,
        },
      },
    },
  );
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

/* 鈹€鈹€ Script visual assets 鈹€鈹€ */

export interface UploadSegmentVisualAssetInput {
  expectedStoryVersion: number;
  expectedScriptRevision: number;
  file: File;
}

export async function listStoryVisualAssets(storyId: string): Promise<StoryVisualAssets> {
  const result = await api.GET('/api/v1/stories/{story_id}/visual-assets', {
    params: {path: {story_id: storyId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

/** Scoped browser URL for a persisted visual asset; it never exposes a host path. */
export function visualAssetContentUrl(storyId: string, assetId: string): string {
  return `/api/v1/stories/${encodeURIComponent(storyId)}/visual-assets/${encodeURIComponent(assetId)}/content`;
}

export async function uploadSegmentVisualAsset(
  storyId: string,
  segmentId: string,
  input: UploadSegmentVisualAssetInput,
): Promise<VisualAssetMutation> {
  const params = new URLSearchParams({
    expected_story_version: String(input.expectedStoryVersion),
    expected_script_revision: String(input.expectedScriptRevision),
    filename: input.file.name,
  });
  const response = await fetch(
    `/api/v1/stories/${encodeURIComponent(storyId)}/visual-assets/${encodeURIComponent(segmentId)}?${params.toString()}`,
    {
      method: 'PUT',
      headers: {'Content-Type': input.file.type},
      body: input.file,
    },
  );
  if (!response.ok) {
    let error: unknown = null;
    try {
      error = await response.json();
    } catch {
      // The common API error helper supplies a stable fallback problem.
    }
    throwProblem(error, response);
  }
  return response.json() as Promise<VisualAssetMutation>;
}

export async function deleteSegmentVisualAsset(
  storyId: string,
  segmentId: string,
  expectedStoryVersion: number,
  expectedScriptRevision: number,
): Promise<void> {
  const result = await api.DELETE('/api/v1/stories/{story_id}/visual-assets/{segment_id}', {
    params: {
      path: {story_id: storyId, segment_id: segmentId},
      query: {
        expected_story_version: expectedStoryVersion,
        expected_script_revision: expectedScriptRevision,
      },
    },
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
}

/* ── Rights-aware visual discovery ── */

export async function searchCommonsVisuals(query: string, limit = 10) {
  const result = await api.GET('/api/v1/visual-discovery/commons', {
    params: {query: {query, limit}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function stageCommonsVisual(body: StageCommonsVisualRequest) {
  const result = await api.POST('/api/v1/visual-discovery/commons/stage', {body});
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function listStoryVisualDiscoveryAssets(storyId: string) {
  const result = await api.GET('/api/v1/stories/{story_id}/visual-discovery-assets', {
    params: {path: {story_id: storyId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function approveVisualDiscoveryAsset(
  assetId: string,
  body: VisualDiscoveryReviewRequest,
) {
  const result = await api.POST('/api/v1/visual-discovery-assets/{asset_id}/approve', {
    params: {path: {asset_id: assetId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function rejectVisualDiscoveryAsset(
  assetId: string,
  body: VisualDiscoveryReviewRequest,
) {
  const result = await api.POST('/api/v1/visual-discovery-assets/{asset_id}/reject', {
    params: {path: {asset_id: assetId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function reuseApprovedVisualDiscoveryAsset(
  assetId: string,
  body: ReuseApprovedVisualRequest,
) {
  const result = await api.POST('/api/v1/visual-discovery-assets/{asset_id}/reuse', {
    params: {path: {asset_id: assetId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export function visualDiscoveryAssetContentUrl(assetId: string): string {
  return `/api/v1/visual-discovery-assets/${encodeURIComponent(assetId)}/content`;
}

/* ── Global media catalog ── */

export async function listMediaCatalogAssets(params?: MediaCatalogListParams) {
  const result = await api.GET('/api/v1/media-assets', {params: {query: params}});
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function archiveMediaCatalogAsset(
  catalogId: string,
  body: ChangeMediaLifecycleRequest,
) {
  const result = await api.POST('/api/v1/media-assets/{catalog_id}/archive', {
    params: {path: {catalog_id: catalogId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function restoreMediaCatalogAsset(
  catalogId: string,
  body: ChangeMediaLifecycleRequest,
) {
  const result = await api.POST('/api/v1/media-assets/{catalog_id}/restore', {
    params: {path: {catalog_id: catalogId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export function mediaCatalogAssetContentUrl(catalogId: string): string {
  return `/api/v1/media-assets/${encodeURIComponent(catalogId)}/content`;
}

/* ── Roles ── */

export async function listRoles(enabled?: boolean | null) {
  const result = await api.GET('/api/v1/roles', {
    params: {query: {enabled}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function getRole(profileId: string) {
  const result = await api.GET('/api/v1/roles/{profile_id}', {
    params: {path: {profile_id: profileId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function createRole(body: RoleProfileCreate) {
  const result = await api.POST('/api/v1/roles', {body});
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function updateRole(profileId: string, body: RoleProfileReplace) {
  const result = await api.PUT('/api/v1/roles/{profile_id}', {
    params: {path: {profile_id: profileId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function deleteRole(profileId: string, expectedVersion: number) {
  const result = await api.DELETE('/api/v1/roles/{profile_id}', {
    params: {path: {profile_id: profileId}},
    body: {expected_version: expectedVersion},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

/* ── Source Runs ── */

export async function getSourceCollectors() {
  const result = await api.GET('/api/v1/sources/collectors');
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function diagnoseSource(source: StartSourceRunRequest['source']) {
  const result = await api.POST('/api/v1/sources/{source}/diagnostics', {
    params: {path: {source}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function startSourceRun(body: StartSourceRunRequest) {
  const result = await api.POST('/api/v1/source-runs', {body});
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function listSourceRuns(params?: SourceRunListParams) {
  const result = await api.GET('/api/v1/source-runs', {params: {query: params}});
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function getSourceRun(runId: string) {
  const result = await api.GET('/api/v1/source-runs/{run_id}', {
    params: {path: {run_id: runId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function getSourceSchedule() {
  const result = await api.GET('/api/v1/source-schedule');
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function startSourceSchedule() {
  const result = await api.POST('/api/v1/source-schedule/start');
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function stopSourceSchedule() {
  const result = await api.POST('/api/v1/source-schedule/stop');
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

/* ── Video ── */

export async function listBgmTracks(): Promise<BgmTrack[]> {
  const result = await api.GET('/api/v1/video/bgm');
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function createVideoBatch(body: CreateVideoBatch) {
  const result = await api.POST('/api/v1/video/batches', {body});
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function listVideoBatches(params?: VideoBatchListParams) {
  const result = await api.GET('/api/v1/video/batches', {params: {query: params}});
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function getVideoBatch(batchId: string) {
  const result = await api.GET('/api/v1/video/batches/{batch_id}', {
    params: {path: {batch_id: batchId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

/** Stable browser URL for one persisted merged-narration audio segment. */
export function videoBatchAudioClipUrl(batchId: string, segmentId: string): string {
  return `/api/v1/video/batches/${encodeURIComponent(batchId)}/audio/${encodeURIComponent(segmentId)}`;
}

/** Stable browser URL for a verified platform render; no host path crosses the API. */
export function videoBatchOutputUrl(batchId: string, profileId: string): string {
  return `/api/v1/video/batches/${encodeURIComponent(batchId)}/outputs/${encodeURIComponent(profileId)}`;
}

export async function submitTimelineReview(batchId: string, body: SubmitTimelineReview) {
  const result = await api.POST('/api/v1/video/batches/{batch_id}/timeline-review', {
    params: {path: {batch_id: batchId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function submitVideoBatchNarrationReview(
  batchId: string,
  body: SubmitNarrationReview,
) {
  const result = await api.POST('/api/v1/video/batches/{batch_id}/narration-review', {
    params: {path: {batch_id: batchId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function synthesizeVideoBatchNarration(
  batchId: string,
  body: SynthesizeBatchNarration,
) {
  const result = await api.POST('/api/v1/video/batches/{batch_id}/narration/synthesize', {
    params: {path: {batch_id: batchId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function renderVideoBatch(batchId: string, body: RenderVideoBatch) {
  const result = await api.POST('/api/v1/video/batches/{batch_id}/render', {
    params: {path: {batch_id: batchId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

/* ── Operations ── */

export async function triggerRetention(body: RetentionCleanupCommand) {
  const result = await api.POST('/api/v1/operations/retention/runs', {body});
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function listOperationRuns(): Promise<OperationRun[]> {
  const result = await api.GET('/api/v1/operations/runs');
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function listSchedules(): Promise<ScheduleSnapshot[]> {
  const result = await api.GET('/api/v1/operations/schedules');
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

/* ── Story lifecycle additions ── */

/** DELETE /api/v1/stories/{story_id} — implemented by the archive lifecycle route. */
export async function deleteStory(storyId: string) {
  const result = await api.DELETE('/api/v1/stories/{story_id}', {
    params: {path: {story_id: storyId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

/** POST /api/v1/stories/{story_id}/reopen — implemented by the lifecycle route. */
export async function reopenStory(storyId: string) {
  const result = await api.POST('/api/v1/stories/{story_id}/reopen', {
    params: {path: {story_id: storyId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function cancelSourceRun(runId: string) {
  const result = await api.POST('/api/v1/source-runs/{run_id}/cancel', {
    params: {path: {run_id: runId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function cancelVideoRender(batchId: string) {
  const result = await api.POST('/api/v1/video/batches/{batch_id}/cancel', {
    params: {path: {batch_id: batchId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}

export async function deleteVideoBatch(batchId: string): Promise<void> {
  const result = await api.DELETE('/api/v1/video/batches/{batch_id}', {
    params: {path: {batch_id: batchId}},
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
}

/** PATCH /api/v1/stories/{story_id} — requires the current story version. */
export async function updateStory(storyId: string, body: StoryUpdate) {
  const result = await api.PATCH('/api/v1/stories/{story_id}', {
    params: {path: {story_id: storyId}},
    body,
  });
  if (result.error !== undefined) throwProblem(result.error, result.response);
  return result.data;
}
