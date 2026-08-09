import {interpolate, OffthreadVideo, useCurrentFrame} from 'remotion';

import {sourceForBrowser} from '../browser-assets';
import type {SceneTrack} from '../render-plan';
import type {GodNewsVideoProps} from '../schema';
import {hostPresetRegistry} from '../templates/presentation-registry';

export const HostRenderer = ({
  props,
  track,
}: {
  props: GodNewsVideoProps;
  track: Extract<SceneTrack, {kind: 'segment'}>;
}) => {
  const frame = useCurrentFrame();
  const template = props.template;
  if (!template) throw new Error('Visible host requires a versioned template.');
  const preset = hostPresetRegistry.resolve(template.host_preset);
  const source = sourceForBrowser(
    props.runtime_assets.host_video_by_segment_id[track.segment.segment_id],
  );
  if (!source) {
    throw new Error(
      `Visible host has no reviewed pre-rendered media: ${track.segment.segment_id}`,
    );
  }
  const enter = track.scene.host_enter
    ? interpolate(frame, [0, preset.enterFrames], [0, 1], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
      })
    : 1;
  const exitStart = Math.max(0, track.durationInFrames - preset.exitFrames);
  const exit = track.scene.host_exit
    ? interpolate(
        frame,
        [exitStart, Math.max(exitStart + 1, track.durationInFrames - 1)],
        [1, 0],
        {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
      )
    : 1;
  const opacity = Math.min(enter, exit);
  return (
    <OffthreadVideo
      data-host-segment-id={track.segment.segment_id}
      src={source}
      muted
      transparent
      style={{
        width: '100%',
        height: '100%',
        objectFit: preset.objectFit,
        opacity,
        transform:
          `translateY(${(1 - enter) * preset.enterOffsetPercent}%) ` +
          `scale(${preset.enterScale + enter * (1 - preset.enterScale)})`,
      }}
    />
  );
};
