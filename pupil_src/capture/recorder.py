'''
(*)~----------------------------------------------------------------------------------
 Pupil - eye tracking platform
 Copyright (C) 2012-2015  Pupil Labs

 Distributed under the terms of the CC BY-NC-SA License.
 License details are in the file license.txt, distributed as part of this software.
----------------------------------------------------------------------------------~(*)
'''

import os, sys, platform
from pyglui import ui
import numpy as np
from scipy.interpolate import UnivariateSpline
from plugin import Plugin
from time import strftime,localtime,time,gmtime
from shutil import copy2
from glob import glob
from audio import Audio_Capture,Audio_Input_Dict
from file_methods import save_object
from av_writer import JPEG_Dumper,ffmpeg_available
from cv2_writer import CV_Writer
#logging
import logging
logger = logging.getLogger(__name__)

import subprocess as sp


def get_auto_name():
    return strftime("%Y_%m_%d", localtime())

def sanitize_timestamps(ts):
    logger.debug("Checking %s timestamps for monotony in direction and smoothness"%ts.shape[0])
    avg_frame_time = (ts[-1] - ts[0])/ts.shape[0]
    logger.debug('average_frame_time: %s'%(1./avg_frame_time))

    raw_ts = ts #only needed for visualization
    runs = 0
    while True:
        #forward check for non monotonic increasing behaviour
        clean = np.ones((ts.shape[0]),dtype=np.bool)
        damper  = 0
        for idx in range(ts.shape[0]-1):
            if ts[idx] >= ts[idx+1]: #not monotonically increasing timestamp
                damper = 50
            clean[idx] = damper <= 0
            damper -=1

        #backward check to smooth timejumps forward
        damper  = 0
        for idx in range(ts.shape[0]-1)[::-1]:
            if ts[idx+1]-ts[idx]>1: #more than one second forward jump
                damper = 50
            clean[idx] &= damper <= 0
            damper -=1

        if clean.all() == True:
            if runs >0:
                logger.debug("Timestamps were bad but are ok now. Correction runs: %s"%runs)
                # from matplotlib import pyplot as plt
                # plt.plot(frames,raw_ts)
                # plt.plot(frames,ts)
                # # plt.scatter(frames[~clean],ts[~clean])
                # plt.show()
            else:
                logger.debug("Timestamps are clean.")
            return ts

        runs +=1
        if runs > 4:
            logger.error("Timestamps could not be fixed!")
            return ts

        logger.warning("Timestamps are not sane. We detected non monotitc or jumpy timestamps. Fixing them now")
        frames = np.arange(len(ts))
        s = UnivariateSpline(frames[clean],ts[clean],s=0)
        ts = s(frames)



class Recorder(Plugin):
    """Capture Recorder"""
    def __init__(self,g_pool,session_name = get_auto_name(),rec_dir=None, user_info={'name':'','additional_field':'change_me'},info_menu_conf={},show_info_menu=False, record_eye = False, audio_src = 'No Audio',raw_jpeg=False):
        super(Recorder, self).__init__(g_pool)
        #update name if it was autogenerated.
        if session_name.startswith('20') and len(session_name)==10:
            session_name = get_auto_name()

        if rec_dir:
            self.set_rec_dir(rec_dir)
        else:
            #lets make a rec dir next to the user dir
            base_dir = self.g_pool.user_dir.rsplit(os.path.sep,1)[0]
            self.rec_dir = os.path.join(base_dir,'recordings')
            if not os.path.isdir(self.rec_dir):
                os.mkdir(self.rec_dir)


        self.raw_jpeg = raw_jpeg
        self.order = .9
        self.record_eye = record_eye
        self.session_name = session_name
        self.audio_devices_dict = Audio_Input_Dict()
        if audio_src in self.audio_devices_dict.keys():
            self.audio_src = audio_src
        else:
            self.audio_src = 'No Audio'
        self.running = False
        self.menu = None
        self.button = None

        self.user_info = user_info
        self.show_info_menu = show_info_menu
        self.info_menu = None
        self.info_menu_conf = info_menu_conf
        self.height, self.width = self.g_pool.capture.frame_size


    def get_init_dict(self):
        d = {}
        d['record_eye'] = self.record_eye
        d['audio_src'] = self.audio_src
        d['session_name'] = self.session_name
        d['user_info'] = self.user_info
        d['info_menu_conf'] = self.info_menu_conf
        d['show_info_menu'] = self.show_info_menu
        d['rec_dir'] = self.rec_dir
        d['raw_jpeg'] = self.raw_jpeg
        return d


    def init_gui(self):
        self.menu = ui.Growing_Menu('Recorder')
        self.g_pool.sidebar.insert(3,self.menu)
        self.menu.append(ui.Info_Text('Pupil recordings are saved like this: "path_to_recordings/recording_session_name/nnn" where "nnn" is an increasing number to avoid overwrites. You can use "/" in your session name to create subdirectories.'))
        self.menu.append(ui.Info_Text('Recordings are saved to "~/pupil_recordings". You can change the path here but note that invalid input will be ignored.'))
        self.menu.append(ui.Text_Input('rec_dir',self,setter=self.set_rec_dir,label='Path to recordings'))
        self.menu.append(ui.Text_Input('session_name',self,setter=self.set_session_name,label='Recording session name'))
        self.menu.append(ui.Switch('show_info_menu',self,on_val=True,off_val=False,label='Request additional user info'))
        if ffmpeg_available():
            self.menu.append(ui.Selector('raw_jpeg',self,selection = [True,False], labels=["bigger file, less CPU", "smaller file, more CPU"],label='compression'))
        else:
            self.menu.append(ui.Info_Text("If you install ffmpeg. Pupil Capture can record using less CPU."))
        self.menu.append(ui.Info_Text('Recording the raw eye video is optional. We use it for debugging.'))
        self.menu.append(ui.Switch('record_eye',self,on_val=True,off_val=False,label='Record eye'))
        self.menu.append(ui.Selector('audio_src',self, selection=self.audio_devices_dict.keys()))

        self.button = ui.Thumb('running',self,setter=self.toggle,label='Record',hotkey='r')
        self.button.on_color[:] = (1,.0,.0,.8)
        self.g_pool.quickbar.insert(1,self.button)


    def deinit_gui(self):
        if self.menu:
            self.g_pool.sidebar.remove(self.menu)
            self.menu = None
        if self.button:
            self.g_pool.quickbar.remove(self.button)
            self.button = None



    def toggle(self, _=None):
        if self.running:
            self.stop()
        else:
            self.start()


    def get_rec_time_str(self):
        rec_time = gmtime(time()-self.start_time)
        return strftime("%H:%M:%S", rec_time)

    def start(self):
        self.timestamps = []
        self.data = {'pupil_positions':[],'gaze_positions':[]}
        self.pupil_pos_list = []
        self.gaze_pos_list = []

        self.frame_count = 0
        self.running = True
        self.menu.read_only = True
        self.start_time = time()

        session = os.path.join(self.rec_dir, self.session_name)
        try:
            os.makedirs(session)
            logger.debug("Created new recordings session dir %s"%session)

        except:
            logger.debug("Recordings session dir %s already exists, using it." %session)

        # set up self incrementing folder within session folder
        counter = 0
        while True:
            self.rec_path = os.path.join(session, "%03d/" % counter)
            try:
                os.mkdir(self.rec_path)
                logger.debug("Created new recording dir %s"%self.rec_path)
                break
            except:
                logger.debug("We dont want to overwrite data, incrementing counter & trying to make new data folder")
                counter += 1

        self.meta_info_path = os.path.join(self.rec_path, "info.csv")

        with open(self.meta_info_path, 'w') as f:
            f.write("Recording Name\t"+self.session_name+ "\n")
            f.write("Start Date\t"+ strftime("%d.%m.%Y", localtime(self.start_time))+ "\n")
            f.write("Start Time\t"+ strftime("%H:%M:%S", localtime(self.start_time))+ "\n")


        if self.audio_src != 'No Audio':
            audio_path = os.path.join(self.rec_path, "world.wav")
            self.audio_writer = Audio_Capture(self.audio_devices_dict[self.audio_src],audio_path)
        else:
            self.audio_writer = None

        self.video_path = os.path.join(self.rec_path, "world.mkv")
        if self.raw_jpeg  and "uvc_capture" in str(self.g_pool.capture.__class__):
            self.writer = JPEG_Dumper(self.video_path)
        # elif 1:
        #     self.writer = av_writer.AV_Writer(self.video_path)
        else:
            self.writer = CV_Writer(self.video_path, float(self.g_pool.capture.frame_rate), self.g_pool.capture.frame_size)
        # positions path to eye process
        if self.record_eye:
            for tx in self.g_pool.eye_tx:
                tx.send((self.rec_path,self.raw_jpeg))

        if self.show_info_menu:
            self.open_info_menu()

    def open_info_menu(self):
        self.info_menu = ui.Growing_Menu('additional Recording Info',size=(300,300),pos=(300,300))
        self.info_menu.configuration = self.info_menu_conf

        def populate_info_menu():
            self.info_menu.elements[:-2] = []
            for name in self.user_info.iterkeys():
                self.info_menu.insert(0,ui.Text_Input(name,self.user_info))

        def set_user_info(new_string):
            self.user_info = new_string
            populate_info_menu()

        populate_info_menu()
        self.info_menu.append(ui.Info_Text('Use the *user info* field to add/remove additional fields and their values. The format must be a valid Python dictionary. For example -- {"key":"value"}. You can add as many fields as you require. Your custom fields will be saved for your next session.'))
        self.info_menu.append(ui.Text_Input('user_info',self,setter=set_user_info,label="User info"))
        self.g_pool.gui.append(self.info_menu)

    def close_info_menu(self):
        if self.info_menu:
            self.info_menu_conf = self.info_menu.configuration
            self.g_pool.gui.remove(self.info_menu)
            self.info_menu = None

    def update(self,frame,events):
        if self.running:
            self.data['pupil_positions'] += events['pupil_positions']
            self.data['gaze_positions'] += events['gaze_positions']
            self.timestamps.append(frame.timestamp)
            self.writer.write_video_frame(frame)
            # self.writer.write_video_frame_yuv422(frame)
            self.frame_count += 1

            # cv2.putText(frame.img, "Frame %s"%self.frame_count,(200,200), cv2.FONT_HERSHEY_SIMPLEX,1,(255,100,100))
            for p in events['pupil_positions']:
                pupil_pos = p['timestamp'],p['confidence'],p['id'],p['norm_pos'][0],p['norm_pos'][1],p['diameter']
                self.pupil_pos_list.append(pupil_pos)

            for g in events.get('gaze_positions',[]):
                gaze_pos = g['timestamp'],g['confidence'],g['norm_pos'][0],g['norm_pos'][1]
                self.gaze_pos_list.append(gaze_pos)

            self.button.status_text = self.get_rec_time_str()

    def stop(self):
        #explicit release of VideoWriter
        self.writer.release()
        self.writer = None

        if self.record_eye:
            for tx in self.g_pool.eye_tx:
                try:
                    tx.send((None,None))
                except:
                    logger.warning("Could not stop eye-recording. Please report this bug!")

        save_object(self.data,os.path.join(self.rec_path, "pupil_data"))

        gaze_list_path = os.path.join(self.rec_path, "gaze_positions.npy")
        np.save(gaze_list_path,np.asarray(self.gaze_pos_list))

        pupil_list_path = os.path.join(self.rec_path, "pupil_positions.npy")
        np.save(pupil_list_path,np.asarray(self.pupil_pos_list))

        timestamps_path = os.path.join(self.rec_path, "world_timestamps.npy")
        ts = sanitize_timestamps(np.array(self.timestamps))
        np.save(timestamps_path,ts)

        try:
            copy2(os.path.join(self.g_pool.user_dir,"surface_definitions"),os.path.join(self.rec_path,"surface_definitions"))
        except:
            logger.info("No surface_definitions data found. You may want this if you do marker tracking.")

        try:
            copy2(os.path.join(self.g_pool.user_dir,"cal_pt_cloud.npy"),os.path.join(self.rec_path,"cal_pt_cloud.npy"))
        except:
            logger.warning("No calibration data found. Please calibrate first.")

        try:
            copy2(os.path.join(self.g_pool.user_dir,"camera_matrix.npy"),os.path.join(self.rec_path,"camera_matrix.npy"))
            copy2(os.path.join(self.g_pool.user_dir,"dist_coefs.npy"),os.path.join(self.rec_path,"dist_coefs.npy"))
        except:
            logger.info("No camera intrinsics found.")

        try:
            with open(self.meta_info_path, 'a') as f:
                f.write("Duration Time\t"+ self.get_rec_time_str()+ "\n")
                if self.g_pool.binocular:
                    f.write("Eye Mode\tbinocular\n")
                else:
                    f.write("Eye Mode\tmonocular\n")
                f.write("Duration Time\t"+ self.get_rec_time_str()+ "\n")
                f.write("World Camera Frames\t"+ str(self.frame_count)+ "\n")
                f.write("World Camera Resolution\t"+ str(self.width)+"x"+str(self.height)+"\n")
                f.write("Capture Software Version\t%s\n"%self.g_pool.version)
                if platform.system() == "Windows":
                    username = os.environ["USERNAME"]
                    sysname, nodename, release, version, machine, _ = platform.uname()
                else:
                    username = os.getlogin()
                    try:
                        sysname, nodename, release, version, machine = os.uname()
                    except:
                        sysname, nodename, release, version, machine = sys.platform,None,None,None,None
                f.write("User\t"+username+"\n")
                f.write("Platform\t"+sysname+"\n")
                f.write("Machine\t"+nodename+"\n")
                f.write("Release\t"+release+"\n")
                f.write("Version\t"+version+"\n")
        except Exception:
            logger.exception("Could not save metadata. Please report this bug!")

        try:
            with open(os.path.join(self.rec_path, "user_info.csv"), 'w') as f:
                for name,val in self.user_info.iteritems():
                    f.write("%s\t%s\n"%(name,val))
        except Exception:
            logger.exception("Could not save userdata. Please report this bug!")


        self.close_info_menu()

        if self.audio_writer:
            self.audio_writer = None

        self.running = False
        self.menu.read_only = False
        self.button.status_text = ''



    def cleanup(self):
        """gets called when the plugin get terminated.
           either volunatily or forced.
        """
        if self.running:
            self.stop()
        self.deinit_gui()



    def set_rec_dir(self,val):
        try:
            n_path = os.path.expanduser(val)
            logger.debug("Expanded user path.")
        except:
            n_path = val
        if not n_path:
            logger.warning("Please specify a path.")
        elif not os.path.isdir(n_path):
            logger.warning("This is not a valid path.")
        # elif not os.access(n_path, os.W_OK):
        elif not writable_dir(n_path):
            logger.warning("Do not have write access to '%s'."%n_path)
        else:
            self.rec_dir = n_path

    def set_session_name(self, val):
        if not val:
            self.session_name = get_auto_name()
        else:
            if '/' in val:
                logger.warning('You session name with create one or more subdirectories')
            self.session_name = val


def writable_dir(n_path):
    try:
         open(os.path.join(n_path,'dummpy_tmp'), 'w')
    except IOError:
         return False
    else:
         os.remove(os.path.join(n_path,'dummpy_tmp'))
         return True


