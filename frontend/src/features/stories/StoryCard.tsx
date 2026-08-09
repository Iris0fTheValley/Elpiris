import {ArrowRight, Clock3, FileWarning, Trash2} from 'lucide-react';
import {Link} from 'react-router-dom';

import type {Story} from '../../api/types';
import {CueRail} from '../../components/CueRail';
import {STATUS_LABELS} from '../../components/cueRailData';
import {ScreeningBadge} from '../../components/ScreeningBadge';

interface StoryCardProps {
  story: Story;
  onDeleteRequest?: (id: string) => void;
}

function formatDate(value?: string): string {
  if (value === undefined) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));
}

export function StoryCard({story, onDeleteRequest}: StoryCardProps) {
  const storyId = story.story_id;
  const displayTitle = story.title ?? story.translation?.translated_title ?? story.source.title;
  const summary = story.translation?.summary ?? story.original_text.slice(0, 180);
  return (
    <article className="story-cue">
      <div className="story-cue-main">
        <div className="source-stamp">
          <span>{story.source.fetcher}</span>
          <small>{story.source.detected_language ?? '语言待识别'}</small>
        </div>
        <div className="story-copy">
          <div className="story-title-line">
            <h2>{displayTitle}</h2>
            <span className="status-chip">{STATUS_LABELS[story.status]}</span>
          </div>
          {displayTitle === story.source.title ? null : (
            <p className="metadata story-source-title">原标题：{story.source.title}</p>
          )}
          <p>{summary}</p>
          <div className="story-meta metadata">
            <span>
              <Clock3 size={14} aria-hidden="true" /> {formatDate(story.updated_at)}
            </span>
            <span>v{String(story.version ?? 1)}</span>
            <span>{story.target_language}</span>
            {story.translation?.screening === undefined ? null : (
              <ScreeningBadge screening={story.translation.screening} />
            )}
          </div>
          {story.last_failure === null || story.last_failure === undefined ? null : (
            <div className="inline-failure">
              <FileWarning size={16} aria-hidden="true" />
              <span>{story.last_failure.message}</span>
              <code>{story.last_failure.code}</code>
            </div>
          )}
        </div>
        <div className="story-actions">
          {storyId === undefined ? (
            <span className="button" aria-disabled="true">
              缺少 ID
            </span>
          ) : (
            <>
              <Link className="button cue-action" to={`/stories/${storyId}`}>
                打开工作台 <ArrowRight size={17} aria-hidden="true" />
              </Link>
              {onDeleteRequest !== undefined && story.status !== 'ARCHIVED' ? (
                <button
                  className="icon-button danger"
                  type="button"
                  aria-label={`删除 ${displayTitle}`}
                  onClick={() => onDeleteRequest(storyId)}
                >
                  <Trash2 size={16} aria-hidden="true" />
                </button>
              ) : null}
            </>
          )}
        </div>
      </div>
      <CueRail compact status={story.status} />
    </article>
  );
}
