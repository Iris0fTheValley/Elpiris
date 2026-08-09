import {screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {beforeEach, describe, expect, it, vi} from 'vitest';

import {StoryListPage} from '@/features/stories/StoryListPage';
import {storyFixture} from '@test/fixtures';
import {renderWithApp} from '@test/render';

const apiMocks = vi.hoisted(() => ({
  listStories: vi.fn(),
  createStory: vi.fn(),
  deleteStory: vi.fn(),
  getClassificationMetrics: vi.fn(),
}));

vi.mock('@/api/client', () => ({
  listStories: apiMocks.listStories,
  createStory: apiMocks.createStory,
  deleteStory: apiMocks.deleteStory,
  getClassificationMetrics: apiMocks.getClassificationMetrics,
}));

describe('StoryListPage', () => {
  beforeEach(() => {
    apiMocks.listStories.mockResolvedValue([storyFixture]);
    apiMocks.getClassificationMetrics.mockResolvedValue({
      reviewed_count: 4,
      accepted_count: 3,
      accuracy: 0.75,
    });
  });

  it('shows the production state and opens an accessible ingest dialog', async () => {
    const user = userEvent.setup();
    renderWithApp(<StoryListPage />);

    expect(await screen.findByText('陌生人把走失的小狗送回了家')).toBeVisible();
    expect(screen.getByText('等待初审', {selector: '.status-chip'})).toBeVisible();
    expect(screen.getByText('1 条')).toBeVisible();
    expect(screen.getByText(/75% \/ 4 次复核/u)).toBeVisible();

    await user.click(screen.getByRole('button', {name: '新建故事'}));
    expect(screen.getByRole('dialog', {name: '加入一条候选内容'})).toBeVisible();
  });

  it('filters the loaded queue by title, source URL, and summary', async () => {
    const user = userEvent.setup();
    apiMocks.listStories.mockResolvedValue([
      storyFixture,
      {
        ...storyFixture,
        story_id: '22222222-2222-4222-8222-222222222222',
        trace_id: '33333333-3333-4333-8333-333333333333',
        title: '社区图书馆重新开放',
        source: {
          ...storyFixture.source,
          title: 'Library update',
          source_uri: 'https://public.example/library',
        },
        translation: {
          ...storyFixture.translation,
          translated_title: '社区图书馆重新开放',
          summary: '志愿者送来了新的图书。',
        },
      },
    ]);
    renderWithApp(<StoryListPage />);

    expect(await screen.findByText('社区图书馆重新开放')).toBeVisible();
    await user.type(screen.getByRole('searchbox', {name: '搜索故事'}), '志愿者');

    expect(screen.getByText('社区图书馆重新开放')).toBeVisible();
    expect(screen.queryByText('陌生人把走失的小狗送回了家')).not.toBeInTheDocument();
    expect(screen.getByText('1 / 2 条')).toBeVisible();
  });

  it('submits only source and target language so review preferences stay server-owned', async () => {
    const user = userEvent.setup();
    apiMocks.createStory.mockResolvedValue(storyFixture);
    renderWithApp(<StoryListPage />);

    await user.click(await screen.findByRole('button', {name: '新建故事'}));
    await user.type(
      screen.getByRole('textbox', {name: /公开 URL/u}),
      'https://example.org/good-news',
    );
    await user.click(screen.getByRole('button', {name: '创建并进入初审'}));

    expect(apiMocks.createStory.mock.calls[0]?.[0]).toEqual({
      source: {kind: 'url', url: 'https://example.org/good-news'},
      target_language: 'zh-CN',
    });
  });
});
