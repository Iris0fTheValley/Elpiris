import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {Filter, Search} from 'lucide-react';
import {useState} from 'react';
import {useSearchParams} from 'react-router-dom';

import {deleteStory, getClassificationMetrics, listStories} from '../../api/client';
import {queryKeys} from '../../api/queryKeys';
import type {StoryStatus} from '../../api/types';
import {ApiErrorNotice} from '../../components/ApiErrorNotice';
import {ConfirmDialog} from '../../components/ConfirmDialog';
import {STATUS_LABELS, STORY_FILTER_STATUSES} from '../../components/cueRailData';
import {useToast} from '../../components/toastContext';
import {CreateStoryForm} from './CreateStoryForm';
import {StoryCard} from './StoryCard';

function isStoryStatus(value: string | null): value is StoryStatus {
  return value !== null && (STORY_FILTER_STATUSES as readonly string[]).includes(value);
}

export function StoryListPage() {
  const [params, setParams] = useSearchParams();
  const queryClient = useQueryClient();
  const {push: pushToast} = useToast();
  const requested = params.get('status');
  const status = isStoryStatus(requested) ? requested : undefined;
  const [search, setSearch] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const deleteMutation = useMutation({
    mutationFn: deleteStory,
    onSettled: () => {
      void queryClient.invalidateQueries({queryKey: queryKeys.stories()});
      setDeleteTarget(null);
    },
    onError: (err) => {
      pushToast({message: `归档失败：${err instanceof Error ? err.message : '未知错误'}`, variant: 'caution', durationMs: 5000});
    },
  });
  const query = useQuery({
    queryKey: queryKeys.stories(status),
    queryFn: () => listStories(status),
    refetchInterval: (state) => {
      const items = state.state.data;
      return items?.some((story) => ['PROCESSING_SCRIPT', 'PROCESSING_TTS'].includes(story.status))
        ? 2_000
        : false;
    },
  });
  const metricsQuery = useQuery({
    queryKey: queryKeys.classificationMetrics(),
    queryFn: getClassificationMetrics,
  });
  const normalizedSearch = search.trim().toLocaleLowerCase();
  const visibleStories = query.data?.filter((story) => {
    if (normalizedSearch === '') return true;
    return [
      story.title,
      story.source.title,
      story.translation?.translated_title,
      story.source.source_uri,
      story.provenance?.source,
      story.translation?.summary,
    ].some((value) => value?.toLocaleLowerCase().includes(normalizedSearch) === true);
  });

  return (
    <div className="page stories-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">TODAY'S RUNDOWN</p>
          <h1>故事队列</h1>
          <p>先看证据，再做决定。高能耗脚本与语音只会在初审批准后启动。</p>
        </div>
        <CreateStoryForm />
      </div>

      <div className="queue-toolbar">
        <label className="filter-control">
          <Filter size={17} aria-hidden="true" />
          <span>状态</span>
          <select
            className="select"
            value={status ?? ''}
            onChange={(event) => {
              const value = event.target.value;
              setParams(value === '' ? {} : {status: value});
            }}
          >
            <option value="">全部状态</option>
            {STORY_FILTER_STATUSES.map((item) => (
              <option key={item} value={item}>
                {STATUS_LABELS[item]}
              </option>
            ))}
          </select>
        </label>
        <div className="search-control" style={{flex: '1 1 200px', maxWidth: 320}}>
          <Search size={16} aria-hidden="true" />
          <input
            className="input"
            type="search"
            data-global-search
            placeholder="搜索标题或来源…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="搜索故事"
          />
        </div>
        <div className="queue-count metadata">
          <span>
            {visibleStories === undefined
              ? '—'
              : normalizedSearch === ''
                ? `${String(visibleStories.length)} 条`
                : `${String(visibleStories.length)} / ${String(query.data?.length ?? 0)} 条`}
          </span>
          <span>
            AI 分类接受率{' '}
            {metricsQuery.data?.accuracy == null
              ? '少于 5 次复核'
              : `${Math.round(metricsQuery.data.accuracy * 100)}% / ${String(metricsQuery.data.reviewed_count)} 次复核`}
          </span>
        </div>
      </div>

      {query.isLoading ? (
        <div className="story-list" aria-label="正在加载故事">
          {[0, 1, 2].map((item) => (
            <div className="story-cue loading-cue" key={item}>
              <div className="skeleton" />
              <div className="skeleton" />
            </div>
          ))}
        </div>
      ) : query.error !== null ? (
        <ApiErrorNotice error={query.error} onRetry={() => void query.refetch()} />
      ) : visibleStories?.length === 0 ? (
        <div className="empty-state">
          <h2>{normalizedSearch === '' ? '这个筛选下还没有故事' : '没有匹配的故事'}</h2>
          <p>
            {normalizedSearch === ''
              ? '新建一条 URL 或文本，系统会把它送到人工初审门前。'
              : '尝试搜索标题、来源、来源 URL 或摘要中的其他关键词。'}
          </p>
        </div>
      ) : (
        <div className="story-list">
          {visibleStories?.map((story) => (
            <StoryCard
              key={story.story_id ?? story.trace_id}
              story={story}
              onDeleteRequest={(id) => setDeleteTarget(id)}
            />
          ))}
        </div>
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="归档故事"
        message="归档会保留来源证据和审计记录，并从默认活跃列表中隐藏。"
        variant="danger"
        confirmLabel="确认归档"
        onConfirm={() => { if (deleteTarget !== null) deleteMutation.mutate(deleteTarget); }}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
