import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {CheckCircle2, RotateCcw} from 'lucide-react';
import {type ChangeEvent, useRef, useState} from 'react';
import {useForm, useWatch} from 'react-hook-form';

import {listRoles, retranslateStory, submitFirstReview} from '../../api/client';
import {queryKeys} from '../../api/queryKeys';
import type {FirstReviewSubmission, SpeechEmotion, Story} from '../../api/types';
import {ApiErrorNotice} from '../../components/ApiErrorNotice';
import {ConfirmDialog} from '../../components/ConfirmDialog';
import {CATEGORY_LABELS} from '../../components/contentCategories';
import {SPEECH_EMOTIONS} from '../../components/narrationOptions';
import {SPOKEN_LANGUAGE_OPTIONS} from '../../components/spokenLanguages';

interface ReviewForm {
  reviewerId: string;
  title: string;
  translation: string;
  summary: string;
  keyPoints: string;
  category: keyof typeof CATEGORY_LABELS;
  candidateRecommendation: boolean;
  style: string;
  duration: number;
  speakerId: string;
  spokenLanguage: string;
  captionLanguage: string;
  speed: number;
  note: string;
}

interface FirstReviewPanelProps {
  story: Story;
}

function resolveEmotion(value: string): SpeechEmotion | null {
  return SPEECH_EMOTIONS.includes(value as SpeechEmotion)
    ? value as SpeechEmotion
    : null;
}

function reviewDefaults(story: Story): ReviewForm {
  return {
    reviewerId: 'local-editor',
    title: story.translation?.translated_title ?? story.title ?? story.source.title,
    translation: story.translation?.translated_text ?? '',
    summary: story.translation?.summary ?? '',
    keyPoints: story.translation?.key_points?.join('\n') ?? '',
    category: story.translation?.screening.category ?? 'kindness',
    candidateRecommendation: story.translation?.screening.candidate_recommendation ?? false,
    style: story.preferences.style,
    duration: story.preferences.target_duration_seconds,
    speakerId: story.preferences.speaker_id,
    spokenLanguage: story.preferences.spoken_language ?? '',
    captionLanguage: story.preferences.caption_language ?? story.target_language,
    speed: story.preferences.speed,
    note: '',
  };
}

export function FirstReviewPanel({story}: FirstReviewPanelProps) {
  const storyId = story.story_id;
  const queryClient = useQueryClient();
  const retryReviewId = useRef<string | null>(null);
  const [pendingSubmission, setPendingSubmission] = useState<{
    decision: 'approve' | 'request_changes';
    values: ReviewForm;
  } | null>(null);
  const rolesQuery = useQuery({
    queryKey: queryKeys.roles(true),
    queryFn: () => listRoles(true),
  });
  const {register, handleSubmit, setValue, reset, control, formState} = useForm<ReviewForm>({
    defaultValues: reviewDefaults(story),
  });
  const selectedSpeakerId = useWatch({control, name: 'speakerId'});
  const eligibleRoles = (rolesQuery.data ?? []).filter(
    (role) => role.enabled && role.tts_enabled && resolveEmotion(role.default_emotion) !== null,
  );
  const selectedRole = eligibleRoles.find((role) => role.speaker_id === selectedSpeakerId);
  const selectedEmotion = selectedRole === undefined
    ? null
    : resolveEmotion(selectedRole.default_emotion);
  const canApprove = rolesQuery.isSuccess && selectedRole !== undefined && selectedEmotion !== null;

  const mutation = useMutation({
    mutationFn: async ({
      decision,
      values,
    }: {
      decision: 'approve' | 'request_changes';
      values: ReviewForm;
    }) => {
      if (storyId === undefined) throw new Error('Story ID is missing.');
      const role = eligibleRoles.find((item) => item.speaker_id === values.speakerId);
      const emotion = role === undefined ? null : resolveEmotion(role.default_emotion);
      if (decision === 'approve' && (role === undefined || emotion === null || !rolesQuery.isSuccess)) {
        throw new Error('Select an enabled, TTS-capable role before approving the first review.');
      }
      retryReviewId.current ??= crypto.randomUUID();
      const body: FirstReviewSubmission = {
        review_id: retryReviewId.current,
        expected_story_version: story.version ?? 1,
        decision,
        reviewer_id: values.reviewerId,
        note: values.note || null,
        corrected_title: values.title,
        corrected_translation: values.translation,
        corrected_summary: values.summary,
        corrected_key_points: values.keyPoints
          .split('\n')
          .map((item) => item.trim())
          .filter(Boolean),
        corrected_category: values.category,
        corrected_candidate_recommendation: values.candidateRecommendation,
        preferences: role === undefined || emotion === null ? null : {
          ...story.preferences,
          style: values.style,
          target_duration_seconds: Number(values.duration),
          speaker_id: role.speaker_id,
          spoken_language: values.spokenLanguage.trim() || role.default_spoken_language,
          caption_language: values.captionLanguage.trim(),
          emotion,
          speed: Number(values.speed),
          pitch: story.preferences.pitch,
        },
      };
      return submitFirstReview(storyId, body);
    },
    onSuccess: async () => {
      retryReviewId.current = null;
      if (storyId === undefined) return;
      await Promise.all([
        queryClient.invalidateQueries({queryKey: queryKeys.story(storyId)}),
        queryClient.invalidateQueries({queryKey: queryKeys.stories()}),
        queryClient.invalidateQueries({queryKey: queryKeys.reviews(storyId)}),
        queryClient.invalidateQueries({queryKey: queryKeys.transitions(storyId)}),
      ]);
    },
  });
  const retranslateMutation = useMutation({
    mutationFn: () => {
      if (storyId === undefined) throw new Error('Story ID is missing.');
      return retranslateStory(storyId, {expected_story_version: story.version ?? 1});
    },
    onSuccess: async (updated) => {
      if (storyId === undefined) return;
      reset(reviewDefaults(updated));
      queryClient.setQueryData(queryKeys.story(storyId), updated);
      await queryClient.invalidateQueries({queryKey: queryKeys.stories()});
    },
  });
  const requestDecision = (decision: 'approve' | 'request_changes') => {
    void handleSubmit((values) => {
      setPendingSubmission({decision, values: {...values}});
    })();
  };
  const pendingDecision = pendingSubmission?.decision ?? null;
  const activeDecision = mutation.isPending ? mutation.variables?.decision : undefined;

  return (
    <form
      className="review-form"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        if (canApprove) requestDecision('approve');
      }}
    >
      <p className="eyebrow">FIRST REVIEW · v{String(story.version ?? 1)}</p>
      <h2>人工初审</h2>
      <p className="review-help">确认事实、译文和关键点。在这里设定脚本参数；批准后只生成口播文本，不会启动本地 TTS。</p>
      <button
        className="button secondary"
        type="button"
        disabled={mutation.isPending || retranslateMutation.isPending}
        onClick={() => retranslateMutation.mutate()}
      >
        <RotateCcw size={17} aria-hidden="true" />
        {retranslateMutation.isPending ? '正在重新翻译…' : '重新翻译原文'}
      </button>
      <label className="field">
        <span>审核人</span>
        <input
          className="input"
          aria-invalid={formState.errors.reviewerId !== undefined}
          {...register('reviewerId', {
            required: '请输入审核人。',
            validate: (value) => value.trim() !== '' || '请输入审核人。',
          })}
        />
        {formState.errors.reviewerId?.message === undefined ? null : (
          <small role="alert">{formState.errors.reviewerId.message}</small>
        )}
      </label>
      <label className="field">
        <span>译文标题</span>
        <input
          className="input"
          aria-invalid={formState.errors.title !== undefined}
          {...register('title', {
            required: '请输入译文标题。',
            validate: (value) => value.trim() !== '' || '请输入译文标题。',
          })}
        />
        {formState.errors.title?.message === undefined ? null : (
          <small role="alert">{formState.errors.title.message}</small>
        )}
      </label>
      <label className="field">
        <span>译文</span>
        <textarea
          className="textarea tall"
          aria-invalid={formState.errors.translation !== undefined}
          {...register('translation', {
            required: '请输入译文。',
            validate: (value) => value.trim() !== '' || '请输入译文。',
          })}
        />
        {formState.errors.translation?.message === undefined ? null : (
          <small role="alert">{formState.errors.translation.message}</small>
        )}
      </label>
      <label className="field">
        <span>摘要</span>
        <textarea
          className="textarea"
          aria-invalid={formState.errors.summary !== undefined}
          {...register('summary', {
            required: '请输入摘要。',
            validate: (value) => value.trim() !== '' || '请输入摘要。',
          })}
        />
        {formState.errors.summary?.message === undefined ? null : (
          <small role="alert">{formState.errors.summary.message}</small>
        )}
      </label>
      <label className="field">
        <span>关键点（每行一条）</span>
        <textarea className="textarea compact" {...register('keyPoints')} />
      </label>
      <div className="form-grid">
        <label className="field">
          <span>人工确认分类</span>
          <select className="select" {...register('category')}>
            {Object.entries(CATEGORY_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <label className="checkbox-field">
          <input type="checkbox" {...register('candidateRecommendation')} />
          <span>可以进入候选池</span>
        </label>
      </div>
      <fieldset className="field-group wide">
        <legend className="eyebrow">SCRIPT SETTINGS</legend>
        <p className="field-hint">只能选择已启用、已完成本地 TTS 配置的角色；音高沿用故事当前值且不在此处显示。</p>
        <div className="form-grid">
          <label className="field">
            <span>播报角色</span>
            <select
              className="select"
              disabled={rolesQuery.isLoading}
              {...register('speakerId', {
                onChange: (event: ChangeEvent<HTMLSelectElement>) => {
                  const role = eligibleRoles.find((item) => item.speaker_id === event.currentTarget.value);
                  if (role !== undefined) {
                    setValue('speed', role.default_speed, {shouldDirty: true});
                    setValue('spokenLanguage', role.default_spoken_language, {shouldDirty: true});
                  }
                },
              })}
            >
              <option value="" disabled>请选择可合成的播报角色</option>
              {selectedSpeakerId !== '' && selectedRole === undefined ? (
                <option value={selectedSpeakerId} disabled>当前角色不可用于本地 TTS</option>
              ) : null}
              {eligibleRoles.map((role) => (
                <option key={role.profile_id ?? role.slug} value={role.speaker_id}>{role.display_name} · {role.speaker_id}</option>
              ))}
            </select>
            <small>{rolesQuery.isLoading ? '正在加载角色…' : rolesQuery.isSuccess ? '仅显示可立即用于本地 TTS 的角色。' : '角色列表不可用，不能批准初审。'}</small>
          </label>
          <label className="field">
            <span>语速</span>
            <input
              className="input"
              type="number"
              min={0.6}
              max={1.65}
              step={0.05}
              aria-invalid={formState.errors.speed !== undefined}
              {...register('speed', {
                valueAsNumber: true,
                required: '请输入语速。',
                min: {value: 0.6, message: '语速不能低于 0.6。'},
                max: {value: 1.65, message: '语速不能高于 1.65。'},
              })}
            />
            {formState.errors.speed?.message === undefined ? null : (
              <small role="alert">{formState.errors.speed.message}</small>
            )}
          </label>
          <label className="field">
            <span>口播语言</span>
            <select className="select mono" {...register('spokenLanguage')}>
              {SPOKEN_LANGUAGE_OPTIONS.map((language) => (
                <option key={language.value} value={language.value}>{language.label}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>字幕语言</span>
            <input
              className="input mono"
              placeholder="zh-CN"
              aria-invalid={formState.errors.captionLanguage !== undefined}
              {...register('captionLanguage', {
                required: '请输入字幕语言。',
                validate: (value) => value.trim() !== '' || '请输入字幕语言。',
              })}
            />
            {formState.errors.captionLanguage?.message === undefined ? null : (
              <small role="alert">{formState.errors.captionLanguage.message}</small>
            )}
          </label>
        </div>
      </fieldset>
      <div className="form-grid">
        <label className="field">
          <span>播报风格</span>
          <input
            className="input"
            aria-invalid={formState.errors.style !== undefined}
            {...register('style', {
              required: '请输入播报风格。',
              validate: (value) => value.trim() !== '' || '请输入播报风格。',
            })}
          />
          {formState.errors.style?.message === undefined ? null : (
            <small role="alert">{formState.errors.style.message}</small>
          )}
        </label>
        <label className="field">
          <span>目标秒数</span>
          <input
            className="input"
            type="number"
            min={5}
            max={600}
            aria-invalid={formState.errors.duration !== undefined}
            {...register('duration', {
              valueAsNumber: true,
              required: '请输入目标秒数。',
              min: {value: 5, message: '目标秒数不能少于 5 秒。'},
              max: {value: 600, message: '目标秒数不能超过 600 秒。'},
            })}
          />
          {formState.errors.duration?.message === undefined ? null : (
            <small role="alert">{formState.errors.duration.message}</small>
          )}
        </label>
      </div>
      <label className="field">
        <span>审核说明</span>
        <textarea className="textarea compact" {...register('note')} />
      </label>
      {rolesQuery.error === null ? null : <ApiErrorNotice error={rolesQuery.error} />}
      {mutation.error === null ? null : <ApiErrorNotice error={mutation.error} />}
      {retranslateMutation.error === null ? null : (
        <ApiErrorNotice error={retranslateMutation.error} />
      )}
      <div className="review-actions">
        <button
          className="button secondary"
          type="button"
          disabled={mutation.isPending || retranslateMutation.isPending || formState.isSubmitting}
          onClick={() => requestDecision('request_changes')}
        >
          <RotateCcw size={17} aria-hidden="true" />
          {activeDecision === 'request_changes' ? '正在保存修改…' : '保存修改，暂不生成'}
        </button>
        <button
          className="button primary"
          type="button"
          disabled={
            mutation.isPending
            || retranslateMutation.isPending
            || formState.isSubmitting
            || !canApprove
          }
          onClick={() => requestDecision('approve')}
        >
          <CheckCircle2 size={18} aria-hidden="true" />
          {activeDecision === 'approve' ? '正在生成口播文本…' : '批准并生成口播文本'}
        </button>
      </div>
      {canApprove ? null : (
        <p className="pending-note" role="status">
          {rolesQuery.isLoading
            ? '正在校验可用角色。'
            : '批准前请选择一个已启用且具备完整本地 TTS 配置的角色。'}
        </p>
      )}
      {mutation.isPending ? (
        <p className="pending-note" role="status" aria-live="polite">
          {activeDecision === 'approve'
            ? '正在生成口播文本。请保持页面打开；中断后可从安全检查点恢复。'
            : '正在保存初审修改。'}
        </p>
      ) : null}
      <ConfirmDialog
        open={pendingSubmission !== null}
        title={pendingDecision === 'approve' ? '批准初审' : '保存修改'}
        message={
          pendingDecision === 'approve'
            ? '批准后将生成口播文本，随后仍需人工审稿并手动启动语音合成。确认继续？'
            : '保存修改后将更新初审记录，但不会推进到脚本生成阶段。'
        }
        confirmLabel={pendingDecision === 'approve' ? '批准并生成口播文本' : '确认保存'}
        onConfirm={() => {
          if (pendingSubmission !== null) mutation.mutate(pendingSubmission);
          setPendingSubmission(null);
        }}
        onCancel={() => setPendingSubmission(null)}
      />
    </form>
  );
}
