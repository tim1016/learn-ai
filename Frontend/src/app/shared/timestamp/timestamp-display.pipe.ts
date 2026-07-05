import { Pipe, PipeTransform } from '@angular/core';
import {
  formatTimestampDisplay,
  type TimestampDisplayMode,
  type TimestampGranularity,
} from './timestamp-display';

@Pipe({
  name: 'timestampDisplay',
})
export class TimestampDisplayPipe implements PipeTransform {
  transform(
    value: number | null | undefined,
    mode: TimestampDisplayMode = 'local',
    granularity: TimestampGranularity = 'datetime',
    localTimeZone?: string,
  ): string {
    return formatTimestampDisplay(value, { mode, granularity, localTimeZone });
  }
}
