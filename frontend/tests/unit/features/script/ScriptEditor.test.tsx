import {fireEvent, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {describe, expect, it, vi} from 'vitest';

import {ScriptEditor} from '@/features/script/ScriptEditor';
import {scriptFixture} from '@test/fixtures';
import {renderWithApp} from '@test/render';

const apiMocks = vi.hoisted(() => ({
  regenerateSpokenSegment: vi.fn(),
}));

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@/api/client');
  return {...actual, regenerateSpokenSegment: apiMocks.regenerateSpokenSegment};
});

describe('ScriptEditor', () => {
  it('reorders segments while restoring a contiguous sequence', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderWithApp(<ScriptEditor script={scriptFixture} onChange={onChange} />);

    await user.click(screen.getByRole('button', {name: '上移第 2 段'}));

    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({
      segments: [
        expect.objectContaining({spoken_text: '但有人停下了脚步。', sequence: 0}),
        expect.objectContaining({spoken_text: '雨下得很大。', sequence: 1}),
      ],
    }));
  });

  it('edits translated captions without changing the TTS text', () => {
    const onChange = vi.fn();
    const bilingual = structuredClone(scriptFixture);
    bilingual.spoken_language = 'en-US';
    bilingual.segments[0].spoken_text = 'It is raining heavily.';
    bilingual.segments[0].spoken_language = 'en-US';
    bilingual.segments[0].captions = [
      {kind: 'verbatim', language: 'en-US', text: 'It is raining heavily.'},
      {kind: 'translation', language: 'zh-CN', text: '雨下得很大。'},
    ];
    renderWithApp(<ScriptEditor script={bilingual} onChange={onChange} />);

    const translation = screen.getByLabelText('翻译字幕 · zh-CN');
    fireEvent.change(translation, {target: {value: '外面正下着大雨。'}});

    const latest = onChange.mock.calls.at(-1)?.[0] as typeof bilingual;
    expect(latest.segments[0].spoken_text).toBe('It is raining heavily.');
    expect(latest.segments[0].captions).toContainEqual({
      kind: 'translation',
      language: 'zh-CN',
      text: '外面正下着大雨。',
    });
  });

  it('places editable captions above read-only foreign speech and regenerates that speech', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const bilingual = structuredClone(scriptFixture);
    bilingual.spoken_language = 'en-US';
    bilingual.segments[0].spoken_text = 'It is raining heavily.';
    bilingual.segments[0].spoken_language = 'en-US';
    bilingual.segments[0].captions = [
      {kind: 'verbatim', language: 'en-US', text: 'It is raining heavily.'},
      {kind: 'translation', language: 'zh-CN', text: '雨下得很大。'},
    ];
    const segmentId = bilingual.segments[0].segment_id ?? '';
    apiMocks.regenerateSpokenSegment.mockResolvedValue({
      segment_id: segmentId,
      spoken_text: 'It is pouring outside.',
      spoken_language: 'en-US',
    });
    const {rerender} = renderWithApp(
      <ScriptEditor
        script={bilingual}
        onChange={onChange}
        storyId="story-a"
        storyVersion={4}
      />,
    );

    const translation = screen.getByLabelText('翻译字幕 · zh-CN');
    const spokenText = screen.getAllByLabelText('口播文本')[0];
    expect(
      translation.compareDocumentPosition(spokenText) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(spokenText).toHaveAttribute('readonly');

    fireEvent.change(translation, {target: {value: '外面正下着倾盆大雨。'}});
    const edited = onChange.mock.calls.at(-1)?.[0] as typeof bilingual;
    rerender(
      <ScriptEditor script={edited} onChange={onChange} storyId="story-a" storyVersion={4} />,
    );
    await user.click(screen.getByRole('button', {name: '重新生成第 1 段口播'}));

    await waitFor(() => expect(apiMocks.regenerateSpokenSegment).toHaveBeenCalledWith(
      'story-a',
      segmentId,
      {
        expected_story_version: 4,
        expected_script_revision: bilingual.revision,
        caption_text: '外面正下着倾盆大雨。',
        caption_language: 'zh-CN',
        spoken_language: 'en-US',
      },
    ));
    await waitFor(() => {
      const regenerated = onChange.mock.calls.at(-1)?.[0] as typeof bilingual;
      expect(regenerated.segments[0].spoken_text).toBe('It is pouring outside.');
      expect(regenerated.segments[0]?.captions).toContainEqual({
        kind: 'verbatim',
        language: 'en-US',
        text: 'It is pouring outside.',
      });
    });
  });

  it('shares one role datalist, exposes scene transitions, and keeps visual hints out of the UI', () => {
    const {container} = renderWithApp(<ScriptEditor script={scriptFixture} onChange={vi.fn()} />);

    expect(container.querySelectorAll('datalist')).toHaveLength(1);
    expect(within(container).getAllByLabelText('过场')).toHaveLength(scriptFixture.segments.length);
    expect(within(container).getAllByText('画面 / 图片')).toHaveLength(scriptFixture.segments.length);
    expect(within(container).queryByText('雨伞和小狗')).not.toBeInTheDocument();
  });

  it('restores the script title as part of undo history', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const {rerender} = renderWithApp(
      <ScriptEditor script={scriptFixture} onChange={onChange} storyId="story-a" />,
    );

    fireEvent.change(screen.getByLabelText('脚本标题'), {target: {value: '新的脚本标题'}});
    const changed = onChange.mock.calls.at(-1)?.[0] as typeof scriptFixture;
    rerender(<ScriptEditor script={changed} onChange={onChange} storyId="story-a" />);
    await user.click(screen.getByRole('button', {name: '撤销 Ctrl+Z'}));

    expect(onChange.mock.calls.at(-1)?.[0]).toEqual(expect.objectContaining({
      title: scriptFixture.title,
      segments: scriptFixture.segments,
    }));
  });

  it('leaves native textarea undo alone', () => {
    const onChange = vi.fn();
    renderWithApp(<ScriptEditor script={scriptFixture} onChange={onChange} />);
    const spokenText = screen.getAllByLabelText('口播文本')[0];

    fireEvent.change(spokenText, {target: {value: '编辑中的口播'}});
    fireEvent.keyDown(spokenText, {key: 'z', ctrlKey: true});

    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it('resets history when the story or script revision changes', () => {
    const onChange = vi.fn();
    const {rerender} = renderWithApp(
      <ScriptEditor script={scriptFixture} onChange={onChange} storyId="story-a" />,
    );
    fireEvent.change(screen.getByLabelText('脚本标题'), {target: {value: '故事 A 草稿'}});
    expect(screen.getByRole('button', {name: '撤销 Ctrl+Z'})).toBeInTheDocument();

    const nextScript = structuredClone(scriptFixture);
    nextScript.revision += 1;
    rerender(<ScriptEditor script={nextScript} onChange={onChange} storyId="story-b" />);

    expect(screen.queryByRole('button', {name: '撤销 Ctrl+Z'})).not.toBeInTheDocument();
  });

  it('allows speed to be cleared while editing and restores a valid default on blur', () => {
    const onChange = vi.fn();
    const {rerender} = renderWithApp(<ScriptEditor script={scriptFixture} onChange={onChange} />);
    const speed = screen.getAllByRole('spinbutton')[0];

    fireEvent.change(speed, {target: {value: ''}});
    const cleared = onChange.mock.calls.at(-1)?.[0] as typeof scriptFixture;
    expect(Number.isNaN(cleared.segments[0].speed)).toBe(true);

    rerender(<ScriptEditor script={cleared} onChange={onChange} />);
    fireEvent.blur(screen.getAllByRole('spinbutton')[0]);
    const restored = onChange.mock.calls.at(-1)?.[0] as typeof scriptFixture;
    expect(restored.segments[0].speed).toBe(1);
  });
});
