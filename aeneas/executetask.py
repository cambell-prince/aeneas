#!/usr/bin/env python
# coding=utf-8

"""
Execute a task, that is, compute the sync map for it.
"""

import numpy
import os
import tempfile

import aeneas.globalconstants as gc
import aeneas.globalfunctions as gf
from aeneas.adjustboundaryalgorithm import AdjustBoundaryAlgorithm
from aeneas.audiofile import AudioFile
from aeneas.dtw import DTWAligner
from aeneas.ffmpegwrapper import FFMPEGWrapper
from aeneas.language import Language
from aeneas.logger import Logger
from aeneas.sd import SD
from aeneas.syncmap import SyncMap, SyncMapFragment, SyncMapHeadTailFormat
from aeneas.synthesizer import Synthesizer
from aeneas.textfile import TextFragment
from aeneas.vad import VAD

__author__ = "Alberto Pettarin"
__copyright__ = """
    Copyright 2012-2013, Alberto Pettarin (www.albertopettarin.it)
    Copyright 2013-2015, ReadBeyond Srl   (www.readbeyond.it)
    Copyright 2015,      Alberto Pettarin (www.albertopettarin.it)
    """
__license__ = "GNU AGPL v3"
__version__ = "1.2.0"
__email__ = "aeneas@readbeyond.it"
__status__ = "Production"

class ExecuteTask(object):
    """
    Execute a task, that is, compute the sync map for it.

    :param task: the task to be executed
    :type  task: :class:`aeneas.task.Task`
    :param logger: the logger object
    :type  logger: :class:`aeneas.logger.Logger`
    """

    TAG = "ExecuteTask"

    def __init__(self, task, logger=None):
        self.task = task
        self.cleanup_info = []
        self.logger = logger
        if self.logger is None:
            self.logger = Logger()

    def _log(self, message, severity=Logger.DEBUG):
        """ Log """
        self.logger.log(message, severity, self.TAG)

    def execute(self):
        """
        Execute the task.
        The sync map produced will be stored inside the task object.

        Return ``True`` if the execution succeeded,
        ``False`` if an error occurred.

        :rtype: bool
        """
        self._log("Executing task")

        # check that we have the AudioFile object
        if self.task.audio_file is None:
            self._log("The task does not seem to have its audio file set", Logger.WARNING)
            return False
        if (
                (self.task.audio_file.audio_length is None) or
                (self.task.audio_file.audio_length <= 0)
            ):
            self._log("The task seems to have an invalid audio file", Logger.WARNING)
            return False

        # check that we have the TextFile object
        if self.task.text_file is None:
            self._log("The task does not seem to have its text file set", Logger.WARNING)
            return False
        if len(self.task.text_file) == 0:
            self._log("The task seems to have no text fragments", Logger.WARNING)
            return False

        self._log("Both audio and text input file are present")
        self.cleanup_info = []

        #TODO refactor what follows

        # real full wave    = the real audio file, converted to WAVE format
        # real trimmed wave = real full wave, possibly with head and/or tail trimmed off
        # synt wave         = WAVE file synthesized from text; it will be aligned to real trimmed wave

        # STEP 0 : convert audio file to real full wave
        self._log("STEP 0 BEGIN")
        result, real_full_handler, real_full_path = self._convert()
        self.cleanup_info.append([real_full_handler, real_full_path])
        if not result:
            self._log("STEP 0 FAILURE")
            self._cleanup()
            return False
        self._log("STEP 0 END")

        # STEP 1 : extract MFCCs from real full wave
        self._log("STEP 1 BEGIN")
        result, real_full_wave_full_mfcc, real_full_wave_length = self._extract_mfcc(real_full_path)
        if not result:
            self._log("STEP 1 FAILURE")
            self._cleanup()
            return False
        self._log("STEP 1 END")

        # STEP 2 : cut head and/or tail off
        #          detecting head/tail if requested, and
        #          overwriting real_path
        #          at the end, read_path will not have the head/tail
        self._log("STEP 2 BEGIN")
        result = self._cut_head_tail(real_full_path)
        real_trimmed_path = real_full_path
        if not result:
            self._log("STEP 2 FAILURE")
            self._cleanup()
            return False
        self._log("STEP 2 END")

        # STEP 3 : synthesize text to wave
        self._log("STEP 3 BEGIN")
        result, synt_handler, synt_path, synt_anchors = self._synthesize()
        self.cleanup_info.append([synt_handler, synt_path])
        if not result:
            self._log("STEP 3 FAILURE")
            self._cleanup()
            return False
        self._log("STEP 3 END")

        # STEP 4 : align waves
        self._log("STEP 4 BEGIN")
        result, wave_map = self._align_waves(real_trimmed_path, synt_path)
        if not result:
            self._log("STEP 4 FAILURE")
            self._cleanup()
            return False
        self._log("STEP 4 END")

        # STEP 5 : align text
        self._log("STEP 5 BEGIN")
        result, text_map = self._align_text(wave_map, synt_anchors)
        if not result:
            self._log("STEP 5 FAILURE")
            self._cleanup()
            return False
        self._log("STEP 5 END")

        # STEP 6 : translate the text_map, possibly putting back the head/tail
        self._log("STEP 6 BEGIN")
        result, translated_text_map = self._translate_text_map(
            text_map,
            real_full_wave_length
        )
        if not result:
            self._log("STEP 6 FAILURE")
            self._cleanup()
            return False
        self._log("STEP 6 END")

        # STEP 7 : adjust boundaries
        self._log("STEP 7 BEGIN")
        result, adjusted_map = self._adjust_boundaries(
            translated_text_map,
            real_full_wave_full_mfcc,
            real_full_wave_length
        )
        if not result:
            self._log("STEP 7 FAILURE")
            self._cleanup()
            return False
        self._log("STEP 7 END")

        # STEP 8 : create syncmap and add it to task
        self._log("STEP 8 BEGIN")
        result = self._create_syncmap(adjusted_map)
        if not result:
            self._log("STEP 8 FAILURE")
            self._cleanup()
            return False
        self._log("STEP 8 END")

        # STEP 9 : cleanup
        self._log("STEP 9 BEGIN")
        self._cleanup()
        self._log("STEP 9 END")
        self._log("Execution completed")
        return True

    def _cleanup(self):
        """
        Remove all temporary files.
        """
        for info in self.cleanup_info:
            handler, path = info
            if handler is not None:
                try:
                    self._log(["Closing handler '%s'...", handler])
                    os.close(handler)
                    self._log("Succeeded")
                except:
                    self._log("Failed")
            if path is not None:
                try:
                    self._log(["Removing path '%s'...", path])
                    os.remove(path)
                    self._log("Succeeded")
                except:
                    self._log("Failed")
        self.cleanup_info = []

    def _convert(self):
        """
        Convert the entire audio file into a ``wav`` file.

        (Head/tail will be cut off later.)

        Return a triple:

        1. a success bool flag
        2. handler of the generated wave file
        3. path of the generated wave file
        """
        self._log("Converting real audio to wav")
        handler = None
        path = None
        try:
            self._log("Creating an output tempfile")
            handler, path = tempfile.mkstemp(
                suffix=".wav",
                dir=gf.custom_tmp_dir()
            )
            self._log("Creating a FFMPEGWrapper")
            ffmpeg = FFMPEGWrapper(logger=self.logger)
            self._log("Converting...")
            ffmpeg.convert(
                input_file_path=self.task.audio_file_path_absolute,
                output_file_path=path)
            self._log("Converting... done")
            self._log("Converting real audio to wav: succeeded")
            return (True, handler, path)
        except Exception as e:
            self._log("Converting real audio to wav: failed")
            self._log(["Message: %s", str(e)])
            return (False, handler, path)

    def _extract_mfcc(self, audio_file_path):
        """
        Extract the MFCCs of the real full wave.
        """
        self._log("Extracting MFCCs from real full wave")
        try:
            audio_file = AudioFile(audio_file_path, logger=self.logger)
            audio_file.extract_mfcc()
            self._log("Extracting MFCCs from real full wave: succeeded")
            return (True, audio_file.audio_mfcc, audio_file.audio_length)
        except Exception as e:
            self._log("Extracting MFCCs from real full wave: failed")
            self._log(["Message: %s", str(e)])
            return (False, None, None)

    def _cut_head_tail(self, audio_file_path):
        """
        Set the audio file head or tail,
        suitably cutting the audio file on disk,
        and setting the corresponding parameters in the task configuration.

        Return a success bool flag
        """
        self._log("Setting head and/or tail")
        try:
            configuration = self.task.configuration
            head_length = configuration.is_audio_file_head_length
            process_length = configuration.is_audio_file_process_length
            detect_head_min = configuration.is_audio_file_detect_head_min
            detect_head_max = configuration.is_audio_file_detect_head_max
            detect_tail_min = configuration.is_audio_file_detect_tail_min
            detect_tail_max = configuration.is_audio_file_detect_tail_max

            # explicit head or process?
            explicit = (head_length is not None) or (process_length is not None)

            # at least one detect parameter?
            detect = (
                (detect_head_min is not None) or
                (detect_head_max is not None) or
                (detect_tail_min is not None) or
                (detect_tail_max is not None)
            )

            if explicit or detect:
                # we need to load the audio data
                audio_file = AudioFile(audio_file_path, logger=self.logger)
                audio_file.load_data()

                if explicit:
                    self._log("Explicit head or process")
                else:
                    self._log("No explicit head or process => detecting head/tail")

                    head = 0.0
                    if (detect_head_min is not None) or (detect_head_max is not None):
                        self._log("Detecting head...")
                        detect_head_min = gf.safe_float(detect_head_min, gc.SD_MIN_HEAD_LENGTH)
                        detect_head_max = gf.safe_float(detect_head_max, gc.SD_MAX_HEAD_LENGTH)
                        self._log(["detect_head_min is %.3f", detect_head_min])
                        self._log(["detect_head_max is %.3f", detect_head_max])
                        sd = SD(audio_file, self.task.text_file, logger=self.logger)
                        head = sd.detect_head(detect_head_min, detect_head_max)
                        self._log(["Detected head: %.3f", head])

                    tail = 0.0
                    if (detect_tail_min is not None) or (detect_tail_max is not None):
                        self._log("Detecting tail...")
                        detect_tail_max = gf.safe_float(detect_tail_max, gc.SD_MAX_TAIL_LENGTH)
                        detect_tail_min = gf.safe_float(detect_tail_min, gc.SD_MIN_TAIL_LENGTH)
                        self._log(["detect_tail_min is %.3f", detect_tail_min])
                        self._log(["detect_tail_max is %.3f", detect_tail_max])
                        sd = SD(audio_file, self.task.text_file, logger=self.logger)
                        tail = sd.detect_tail(detect_tail_min, detect_tail_max)
                        self._log(["Detected tail: %.3f", tail])

                    # sanity check
                    head_length = max(0, head)
                    process_length = max(0, audio_file.audio_length - tail - head)

                    # we need to set these values
                    # in the config object for later use
                    self.task.configuration.is_audio_file_head_length = head_length
                    self.task.configuration.is_audio_file_process_length = process_length
                    self._log(["Set head_length:    %.3f", head_length])
                    self._log(["Set process_length: %.3f", process_length])

                if head_length is not None:
                    # in case we are reading from config object
                    head_length = float(head_length)
                if process_length is not None:
                    # in case we are reading from config object
                    process_length = float(process_length)
                # note that str() is necessary, as one might be None
                self._log(["is_audio_file_head_length is %s", str(head_length)])
                self._log(["is_audio_file_process_length is %s", str(process_length)])
                self._log("Trimming audio data...")
                audio_file.trim(head_length, process_length)
                self._log("Trimming audio data... done")
                self._log("Writing audio file...")
                audio_file.write(audio_file_path)
                self._log("Writing audio file... done")
                audio_file.clear_data()
            else:
                # nothing to do
                self._log("No explicit head/process or detect head/tail")

            self._log("Setting head and/or tail: succeeded")
            return True
        except Exception as e:
            self._log("Setting head and/or tail: failed")
            self._log(["Message: %s", str(e)])
            return False

    def _synthesize(self):
        """
        Synthesize text into a ``wav`` file.

        Return a quadruple:

        1. a success bool flag
        2. handler of the generated wave file
        3. path of the generated wave file
        4. the list of anchors, that is, a list of floats
           each representing the start time of the corresponding
           text fragment in the generated wave file
           ``[start_1, start_2, ..., start_n]``
        """
        self._log("Synthesizing text")
        handler = None
        path = None
        anchors = None
        try:
            self._log("Creating an output tempfile")
            handler, path = tempfile.mkstemp(
                suffix=".wav",
                dir=gf.custom_tmp_dir()
            )
            self._log("Creating Synthesizer object")
            synt = Synthesizer(logger=self.logger)
            self._log("Synthesizing...")
            result = synt.synthesize(self.task.text_file, path)
            anchors = result[0]
            self._log("Synthesizing... done")
            self._log("Synthesizing text: succeeded")
            return (True, handler, path, anchors)
        except Exception as e:
            self._log("Synthesizing text: failed")
            self._log(["Message: %s", str(e)])
            return (False, handler, path, anchors)

    def _align_waves(self, real_path, synt_path):
        """
        Align two ``wav`` files.

        Return a pair:

        1. a success bool flag
        2. the computed alignment map, that is,
           a list of pairs of floats, each representing
           corresponding time instants
           in the real and synt wave, respectively
           ``[real_time, synt_time]``
        """
        self._log("Aligning waves")
        try:
            self._log("Creating DTWAligner object")
            aligner = DTWAligner(real_path, synt_path, logger=self.logger)
            self._log("Computing MFCC...")
            aligner.compute_mfcc()
            self._log("Computing MFCC... done")
            self._log("Computing path...")
            aligner.compute_path()
            self._log("Computing path... done")
            self._log("Computing map...")
            computed_map = aligner.computed_map
            self._log("Computing map... done")
            self._log("Aligning waves: succeeded")
            return (True, computed_map)
        except Exception as e:
            self._log("Aligning waves: failed")
            self._log(["Message: %s", str(e)])
            return (False, None)

    def _align_text(self, wave_map, synt_anchors):
        """
        Align the text with the real wave,
        using the ``wave_map`` (containing the mapping
        between real and synt waves) and ``synt_anchors``
        (containing the start times of text fragments
        in the synt wave).

        Return a pair:

        1. a success bool flag
        2. the computed interval map, that is,
           a list of triples ``[start_time, end_time, fragment_id]``
        """
        self._log("Aligning text")
        self._log(["Number of frames:    %d", len(wave_map)])
        self._log(["Number of fragments: %d", len(synt_anchors)])
        try:
            real_times = numpy.array([t[0] for t in wave_map])
            synt_times = numpy.array([t[1] for t in wave_map])
            real_anchors = []
            anchor_index = 0
            # TODO numpy-fy this loop
            for anchor in synt_anchors:
                time, fragment_id, fragment_text = anchor
                self._log("Looking for argmin index...")
                # TODO allow an user-specified function instead of min
                # partially solved by AdjustBoundaryAlgorithm
                index = (numpy.abs(synt_times - time)).argmin()
                self._log("Looking for argmin index... done")
                real_time = real_times[index]
                real_anchors.append([real_time, fragment_id, fragment_text])
                self._log(["Time for anchor %d: %f", anchor_index, real_time])
                anchor_index += 1

            # dummy last anchor, starting at the real file duration
            real_anchors.append([real_times[-1], None, None])

            # compute map
            self._log("Computing interval map...")
            # TODO numpy-fy this loop
            computed_map = []
            for i in range(len(real_anchors) - 1):
                fragment_id = real_anchors[i][1]
                fragment_text = real_anchors[i][2]
                start = real_anchors[i][0]
                end = real_anchors[i+1][0]
                computed_map.append([start, end, fragment_id, fragment_text])
            self._log("Computing interval map... done")
            self._log("Aligning text: succeeded")
            return (True, computed_map)
        except Exception as e:
            self._log("Aligning text: failed")
            self._log(["Message: %s", str(e)])
            return (False, None)

    def _translate_text_map(self, text_map, real_full_wave_length):
        """
        Translate the text_map by adding head and tail dummy fragments
        """
        if len(text_map) == 0:
            self._log("No fragments in the text_map", Logger.CRITICAL)
            return (False, None)
        translated = []
        head = gf.safe_float(self.task.configuration.is_audio_file_head_length, 0)
        translated.append([0, head, None, None])
        end = 0
        for element in text_map:
            start, end, fragment_id, fragment_text = element
            start += head
            end += head
            translated.append([start, end, fragment_id, fragment_text])
        translated.append([end, real_full_wave_length, None, None])
        return (True, translated)

    def _adjust_boundaries(
            self,
            text_map,
            real_wave_full_mfcc,
            real_wave_length
        ):
        """
        Adjust the boundaries between consecutive fragments.

        Return a pair:

        1. a success bool flag
        2. the computed interval map, that is,
           a list of triples ``[start_time, end_time, fragment_id]``

        """
        self._log("Adjusting boundaries")
        algo = self.task.configuration.adjust_boundary_algorithm
        value = None
        if algo is None:
            self._log("No adjust boundary algorithm specified: returning")
            return (True, text_map)
        elif algo == AdjustBoundaryAlgorithm.AUTO:
            self._log("Requested adjust boundary algorithm AUTO: returning")
            return (True, text_map)
        elif algo == AdjustBoundaryAlgorithm.AFTERCURRENT:
            value = self.task.configuration.adjust_boundary_aftercurrent_value
        elif algo == AdjustBoundaryAlgorithm.BEFORENEXT:
            value = self.task.configuration.adjust_boundary_beforenext_value
        elif algo == AdjustBoundaryAlgorithm.OFFSET:
            value = self.task.configuration.adjust_boundary_offset_value
        elif algo == AdjustBoundaryAlgorithm.PERCENT:
            value = self.task.configuration.adjust_boundary_percent_value
        elif algo == AdjustBoundaryAlgorithm.RATE:
            value = self.task.configuration.adjust_boundary_rate_value
        elif algo == AdjustBoundaryAlgorithm.RATEAGGRESSIVE:
            value = self.task.configuration.adjust_boundary_rate_value
        self._log(["Requested algo %s and value %s", algo, value])

        try:
            self._log("Running VAD...")
            vad = VAD(logger=self.logger)
            vad.wave_mfcc = real_wave_full_mfcc
            vad.wave_len = real_wave_length
            vad.compute_vad()
            self._log("Running VAD... done")
        except Exception as e:
            self._log("Adjusting boundaries: failed")
            self._log(["Message: %s", str(e)])
            return (False, None)

        self._log("Creating AdjustBoundaryAlgorithm object")
        adjust_boundary = AdjustBoundaryAlgorithm(
            algorithm=algo,
            text_map=text_map,
            speech=vad.speech,
            nonspeech=vad.nonspeech,
            value=value,
            logger=self.logger
        )
        self._log("Adjusting boundaries...")
        adjusted_map = adjust_boundary.adjust()
        self._log("Adjusting boundaries... done")
        self._log("Adjusting boundaries: succeeded")
        return (True, adjusted_map)

    def _create_syncmap(self, adjusted_map):
        """
        Create a sync map out of the provided interval map,
        and store it in the task object.

        Return a success bool flag.
        """
        self._log("Creating sync map")
        self._log(["Number of fragments in adjusted map (including HEAD and TAIL): %d", len(adjusted_map)])
        # adjusted map has 2 elements (HEAD and TAIL) more than text_file
        if len(adjusted_map) != len(self.task.text_file.fragments) + 2:
            self._log("The number of sync map fragments does not match the number of text fragments (+2)", Logger.CRITICAL)
            return False
        try:
            sync_map = SyncMap()
            head = adjusted_map[0]
            tail = adjusted_map[-1]

            # get language
            language = Language.EN
            self._log(["Language set to default: %s", language])
            if len(self.task.text_file.fragments) > 0:
                language = self.task.text_file.fragments[0].language
                self._log(["Language read from text_file: %s", language])

            # get head/tail format
            head_tail_format = self.task.configuration.os_file_head_tail_format
            # note that str() is necessary, as head_tail_format might be None
            self._log(["Head/tail format: %s", str(head_tail_format)])

            # add head sync map fragment if needed
            if head_tail_format == SyncMapHeadTailFormat.ADD:
                head_frag = TextFragment(u"HEAD", language, [u""])
                sync_map_frag = SyncMapFragment(head_frag, head[0], head[1])
                sync_map.append(sync_map_frag)
                self._log(["Adding head (ADD): %.3f %.3f", head[0], head[1]])

            # stretch first and last fragment timings if needed
            if head_tail_format == SyncMapHeadTailFormat.STRETCH:
                self._log(["Stretching (STRETCH): %.3f => %.3f (head) and %.3f => %.3f (tail)", adjusted_map[1][0], head[0], adjusted_map[-2][1], tail[1]])
                adjusted_map[1][0] = head[0]
                adjusted_map[-2][1] = tail[1]

            i = 1
            for fragment in self.task.text_file.fragments:
                start = adjusted_map[i][0]
                end = adjusted_map[i][1]
                sync_map_frag = SyncMapFragment(fragment, start, end)
                sync_map.append(sync_map_frag)
                i += 1

            # add tail sync map fragment if needed
            if head_tail_format == SyncMapHeadTailFormat.ADD:
                tail_frag = TextFragment(u"TAIL", language, [u""])
                sync_map_frag = SyncMapFragment(tail_frag, tail[0], tail[1])
                sync_map.append(sync_map_frag)
                self._log(["Adding tail (ADD): %.3f %.3f", tail[0], tail[1]])

            self.task.sync_map = sync_map
            self._log("Creating sync map: succeeded")
            return True
        except Exception as e:
            self._log("Creating sync map: failed")
            self._log(["Message: %s", str(e)])
            return False



