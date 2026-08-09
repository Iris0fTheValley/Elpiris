import {cleanup, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';

import type {RoleProfile} from '@/api/types';
import {FirstReviewPanel} from '@/features/reviews/FirstReviewPanel';
import {storyFixture} from '@test/fixtures';
import {renderWithApp} from '@test/render';

const apiMocks = vi.hoisted(() => ({
  listRoles: vi.fn(),
  retranslateStory: vi.fn(),
  submitFirstReview: vi.fn(),
}));

vi.mock('@/api/client', () => ({
  listRoles: apiMocks.listRoles,
  retranslateStory: apiMocks.retranslateStory,
  submitFirstReview: apiMocks.submitFirstReview,
}));

const ttsRole: RoleProfile = {
  profile_id: 'c30b6fd9-9eae-43cb-b6c9-d05b4ce4d11a',
  slug: 'narrator',
  display_name: '默认旁白',
  kind: 'narrator',
  speaker_id: 'narrator',
  character_prompt: 'A clear news narrator.',
  default_emotion: 'happiness',
  default_spoken_language: 'en-US',
  default_speed: 1,
  default_pitch: 0,
  gpt_weights_path: 'voices/narrator.ckpt',
  sovits_weights_path: 'voices/narrator.pth',
  tts_model_profile: 'v2Pro',
  reference_language: 'all_zh',
  emotion_refs: {},
  tts_enabled: true,
  visual_assets: {},
  enabled: true,
  version: 1,
};

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

describe('FirstReviewPanel', () => {
  it('regenerates an unreviewed translation and refreshes the editable fields', async () => {
    const user = userEvent.setup();
    const updated = {
      ...storyFixture,
      version: 4,
      translation: {
        ...storyFixture.translation!,
        translated_text: 'NASA 已重新生成经过语言校验的中文译文。',
        summary: '重新生成的中文摘要。',
      },
    };
    apiMocks.listRoles.mockResolvedValue([ttsRole]);
    apiMocks.retranslateStory.mockResolvedValue(updated);
    renderWithApp(<FirstReviewPanel story={storyFixture} />);

    await user.click(screen.getByRole('button', {name: '重新翻译原文'}));

    await waitFor(() => expect(apiMocks.retranslateStory).toHaveBeenCalledWith(
      storyFixture.story_id,
      {expected_story_version: 3},
    ));
    expect(screen.getByLabelText('译文')).toHaveValue(updated.translation.translated_text);
    expect(screen.getByLabelText('摘要')).toHaveValue(updated.translation.summary);
  });

  it('submits human-corrected key points with an idempotency id', async () => {
    const user = userEvent.setup();
    apiMocks.listRoles.mockResolvedValue([ttsRole]);
    apiMocks.submitFirstReview.mockResolvedValue(storyFixture);
    renderWithApp(<FirstReviewPanel story={storyFixture} />);

    const spokenLanguage = screen.getByRole('combobox', {name: '口播语言'});
    expect(within(spokenLanguage).getAllByRole('option').map((option) => option.textContent)).toEqual([
      '中文',
      '日语',
      '英语',
      '法语',
    ]);
    const keyPoints = screen.getByLabelText('关键点（每行一条）');
    await user.clear(keyPoints);
    await user.type(keyPoints, '核对原始证据\n主人已确认接回');
    await waitFor(() => expect(screen.getByRole('button', {name: '批准并生成口播文本'})).toBeEnabled());
    await user.click(screen.getByRole('button', {name: '批准并生成口播文本'}));
    // ConfirmDialog opens — confirm the action
    const confirmButtons = screen.getAllByRole('button', {name: '批准并生成口播文本'});
    await user.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() => expect(apiMocks.submitFirstReview).toHaveBeenCalledOnce());
    const [storyId, payload] = apiMocks.submitFirstReview.mock.calls[0] as [string, Record<string, unknown>];
    expect(storyId).toBe(storyFixture.story_id);
    expect(payload).toMatchObject({
      decision: 'approve',
      expected_story_version: 3,
      corrected_key_points: ['核对原始证据', '主人已确认接回'],
    });
    expect(payload.preferences).toMatchObject({
      speaker_id: 'narrator',
      emotion: 'happiness',
      spoken_language: 'en-US',
      caption_language: 'zh-CN',
    });
    expect(payload.review_id).toEqual(expect.any(String));
  });

  it('does not permit approval until a loaded role is enabled and TTS-capable', async () => {
    apiMocks.listRoles.mockResolvedValue([{...ttsRole, tts_enabled: false}]);
    renderWithApp(<FirstReviewPanel story={storyFixture} />);

    await waitFor(() => expect(screen.getByText('仅显示可立即用于本地 TTS 的角色。')).toBeVisible());
    expect(screen.getByRole('button', {name: '批准并生成口播文本'})).toBeDisabled();
    expect(screen.getByText('批准前请选择一个已启用且具备完整本地 TTS 配置的角色。')).toBeVisible();
  });

  it('does not replace an unsupported legacy emotion with the story preference', async () => {
    apiMocks.listRoles.mockResolvedValue([{...ttsRole, default_emotion: 'neutral'}]);
    renderWithApp(<FirstReviewPanel story={storyFixture} />);

    await waitFor(() => expect(screen.getByText('仅显示可立即用于本地 TTS 的角色。')).toBeVisible());
    expect(screen.getByRole('button', {name: '批准并生成口播文本'})).toBeDisabled();
    expect(screen.getByRole('option', {name: '当前角色不可用于本地 TTS'})).toBeDisabled();
  });

  it('does not open confirmation or submit when required review text is blank', async () => {
    const user = userEvent.setup();
    apiMocks.listRoles.mockResolvedValue([ttsRole]);
    renderWithApp(<FirstReviewPanel story={storyFixture} />);

    await waitFor(() => expect(screen.getByRole('button', {name: '批准并生成口播文本'})).toBeEnabled());
    await user.clear(screen.getByLabelText('审核人'));
    await user.click(screen.getByRole('button', {name: '批准并生成口播文本'}));

    expect(await screen.findByText('请输入审核人。')).toBeVisible();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(apiMocks.submitFirstReview).not.toHaveBeenCalled();
  });

  it('validates numeric ranges before opening the request-changes confirmation', async () => {
    const user = userEvent.setup();
    apiMocks.listRoles.mockResolvedValue([ttsRole]);
    renderWithApp(<FirstReviewPanel story={storyFixture} />);

    const duration = screen.getByLabelText('目标秒数');
    await user.clear(duration);
    await user.type(duration, '4');
    await user.click(screen.getByRole('button', {name: '保存修改，暂不生成'}));

    expect(await screen.findByText('目标秒数不能少于 5 秒。')).toBeVisible();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(apiMocks.submitFirstReview).not.toHaveBeenCalled();
  });

  it('keeps request-changes behind confirmation and submits validated values', async () => {
    const user = userEvent.setup();
    apiMocks.listRoles.mockResolvedValue([ttsRole]);
    apiMocks.submitFirstReview.mockResolvedValue(storyFixture);
    renderWithApp(<FirstReviewPanel story={storyFixture} />);

    await user.click(screen.getByRole('button', {name: '保存修改，暂不生成'}));
    expect(await screen.findByRole('dialog', {name: '保存修改'})).toBeVisible();
    expect(apiMocks.submitFirstReview).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', {name: '确认保存'}));

    await waitFor(() => expect(apiMocks.submitFirstReview).toHaveBeenCalledOnce());
    expect(apiMocks.submitFirstReview.mock.calls[0]?.[1]).toMatchObject({
      decision: 'request_changes',
      reviewer_id: 'local-editor',
      corrected_translation: storyFixture.translation?.translated_text,
      corrected_summary: storyFixture.translation?.summary,
    });
  });

  it('disables both review actions while a confirmed submission is pending', async () => {
    const user = userEvent.setup();
    let resolveSubmission: ((value: typeof storyFixture) => void) | undefined;
    apiMocks.listRoles.mockResolvedValue([ttsRole]);
    apiMocks.submitFirstReview.mockImplementation(() => new Promise((resolve) => {
      resolveSubmission = resolve;
    }));
    renderWithApp(<FirstReviewPanel story={storyFixture} />);

    await waitFor(() => expect(screen.getByRole('button', {name: '批准并生成口播文本'})).toBeEnabled());
    await user.click(screen.getByRole('button', {name: '批准并生成口播文本'}));
    const confirmButtons = await screen.findAllByRole('button', {name: '批准并生成口播文本'});
    await user.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() => {
      expect(screen.getByRole('button', {name: '正在生成口播文本…'})).toBeDisabled();
      expect(screen.getByRole('button', {name: '保存修改，暂不生成'})).toBeDisabled();
    });

    resolveSubmission?.(storyFixture);
    await waitFor(() => expect(screen.queryByText('正在生成口播文本。请保持页面打开；中断后可从安全检查点恢复。')).not.toBeInTheDocument());
  });
});
