import os
import sys
import pickle
import config as cfg
import numpy as np
import ipdb
import math

def f1_score_max(gt, pred, thresh):
  from sklearn.metrics import precision_score, recall_score
  #P, R, thresh = precision_recall_curve(gt, pred)
  #F1 = 2*P*R/(P+R)
  #F1_ = [n for n in F1 if not math.isnan(n)]

  P=[];R=[]
  for i in thresh:
    new_pred = ((pred>=i)*1).flatten()
    P.append(precision_score(gt.flatten(), new_pred))
    R.append(recall_score(gt.flatten(), new_pred))
  P = np.array(P).flatten()
  R = np.array(R).flatten()
  F1 = 2*P*R/(P+R)
  F1_MAX = max(F1)
  if F1_MAX<0 or math.isnan(F1_MAX): 
    F1_MAX=0
    F1_THRESH=0
  else:
    idx_thresh = np.argmax(F1)
    F1_THRESH = thresh[idx_thresh]

  return F1, F1_MAX, F1_THRESH

def f1_score(gt, pred, F1_Thresh=0.5, median=False):
  import pandas
  from sklearn.metrics import precision_score, recall_score
  from sklearn.metrics import f1_score as f1s
  if type(gt)==list: gt = np.array(gt)
  if type(pred)==list: pred = np.array(pred)
  # F1_Thresh = 0.5
  output = (pred>F1_Thresh)*1.0
  F1 = f1s(gt, output)
  F1_MAX=F1

  if median:
    # ipdb.set_trace()
    output_median3 = np.array(pandas.Series(output).rolling(window=3, center=True).median().bfill().ffill())
    F1_median3 = f1s(gt, output_median3)

    output_median5 = np.array(pandas.Series(output).rolling(window=5, center=True).median().bfill().ffill())
    F1_median5 = f1s(gt, output_median5)

    output_median7 = np.array(pandas.Series(output).rolling(window=7, center=True).median().bfill().ffill())
    F1_median7 = f1s(gt, output_median7)

    return [F1], F1_MAX, F1_Thresh, F1_median3, F1_median5, F1_median7
  else:
    return [F1], F1_MAX, F1_Thresh 

def F1_TEST(config, data_loader, mode = 'TEST', thresh = [0.5]*len(cfg.AUs), show_fake='', verbose=True):
  import torch
  import torch.nn as nn
  import torch.nn.functional as F
  PREDICTION = []
  GROUNDTRUTH = []
  total_idx=int(len(data_loader)/config.batch_size)  
  count = 0
  loss = []
  for i, (real_x, org_c, files) in enumerate(data_loader):

    if mode=='TEST' and not config.GOOGLE and i<2: 
      config.save_fake_output(real_x, show_fake.format(mode.lower(), i))

    if mode!='VAL' and os.path.isfile(config.pkl_data.format(mode.lower())): 
      PREDICTION, GROUNDTRUTH = pickle.load(open(config.pkl_data.format(mode.lower())))
      break
    # ipdb.set_trace()
    real_x = config.to_var(real_x, volatile=True)
    labels = org_c

    ######################################################
    if config.GOOGLE:
      labels_dummy = config.to_var(org_c, volatile=True)

      fake_c=labels_dummy.clone()*0
      fake_list = [fake_c.clone()]
      for i in range(12):
        # fake_c=labels_dummy.clone()*0
        fake_c[:,i]=1

        if config.CelebA_loader is not None:
          zero2 = torch.zeros(real_x.size(0), config.c2_dim)
          mask1 = config.one_hot(torch.zeros(real_x.size(0)), 2)
          zero2 = config.to_var(zero2, volatile=True)           
          mask1 = config.to_var(mask1, volatile=True)
          fake_c_ = torch.cat([fake_c.clone(), zero2, mask1], dim=1)
        else:
          fake_c_ = fake_c.clone()

        fake_list.append(fake_c_)
      config.show_img(real_x, labels_dummy, fake_list, ppt=config.PPT)
      sys.exit("Done")      
    ######################################################
    
    _, out_cls_temp, lstm_input = config.D(real_x, lstm=True)
    if config.CelebA_loader is not None: out_cls_temp = out_cls_temp[:,:config.c_dim]
    if mode!='VAL': config.save_lstm(lstm_input.data.cpu().numpy(), files)
    # output = ((F.sigmoid(out_cls_temp)>=0.5)*1.).data.cpu().numpy()
    output = F.sigmoid(out_cls_temp)

    loss.append(F.binary_cross_entropy_with_logits(
      out_cls_temp, config.to_var(org_c), size_average=False) / org_c.size(0))

    if i==0 and verbose:
      print(mode.upper())
      print("Predicted:   "+str((output>=0.5)*1))
      print("Groundtruth: "+str(org_c))

    count += org_c.shape[0]
    if verbose:
      string_ = str(count)+' / '+str(len(data_loader)*config.batch_size)
      sys.stdout.write("\r%s" % string_)
      sys.stdout.flush()    
    # ipdb.set_trace()

    PREDICTION.append(output.data.cpu().numpy().tolist())
    GROUNDTRUTH.append(labels.cpu().numpy().astype(np.uint8).tolist())

  if mode!='VAL' and not os.path.isfile(config.pkl_data.format(mode.lower())): 
    pickle.dump([PREDICTION, GROUNDTRUTH], open(config.pkl_data.format(mode.lower()), 'w'))
  if verbose: print("")
  print >>config.f, ""
  # print("[Min and Max predicted: "+str(min(prediction))+ " " + str(max(prediction))+"]")
  # print >>config.f, "[Min and Max predicted: "+str(min(prediction))+ " " + str(max(prediction))+"]"
  if verbose: print("")

  PREDICTION = np.vstack(PREDICTION)
  GROUNDTRUTH = np.vstack(GROUNDTRUTH)

  F1_real5 = [0]*len(cfg.AUs); F1_Thresh5 = [0]*len(cfg.AUs); F1_real = [0]*len(cfg.AUs)
  F1_Thresh = [0]*len(cfg.AUs); F1_0 = [0]*len(cfg.AUs); F1_1 = [0]*len(cfg.AUs)
  F1_Thresh_0 = [0]*len(cfg.AUs); F1_Thresh_1 = [0]*len(cfg.AUs); F1_MAX = [0]*len(cfg.AUs)
  F1_Thresh_max = [0]*len(cfg.AUs); F1_median5 = [0]*len(cfg.AUs); F1_median7 = [0]*len(cfg.AUs)
  F1_median3 = [0]*len(cfg.AUs); F1_median3_th = [0]*len(cfg.AUs); F1_median5_th = [0]*len(cfg.AUs);
  F1_median7_th = [0]*len(cfg.AUs);
  # ipdb.set_trace()
  for i in xrange(len(cfg.AUs)):
    prediction = PREDICTION[:,i]
    groundtruth = GROUNDTRUTH[:,i]
    if mode=='TEST':
      _, F1_real5[i], F1_Thresh5[i], F1_median3[i], F1_median5[i], F1_median7[i] = f1_score(groundtruth, prediction, 0.5, median=True)   
    _, F1_real[i], F1_Thresh[i] = f1_score(np.array(groundtruth), np.array(prediction), thresh[i])
    _, F1_0[i], F1_Thresh_0[i] = f1_score(np.array(groundtruth), np.array(prediction)*0, thresh[i])
    _, F1_1[i], F1_Thresh_1[i] = f1_score(np.array(groundtruth), (np.array(prediction)*0)+1, thresh[i])
    _, F1_MAX[i], F1_Thresh_max[i] = f1_score_max(np.array(groundtruth), np.array(prediction), config.thresh)  


  for i, au in enumerate(cfg.AUs):
    string = "---> [%s - 0] AU%s F1: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_0[i], F1_Thresh_0[i])
    if verbose: print(string)
    print >>config.f, string
  string = "F1 Mean: %.4f\n"%np.mean(F1_0)
  if verbose: print(string)
  print >>config.f, string

  for i, au in enumerate(cfg.AUs):
    string = "---> [%s - 1] AU%s F1: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_1[i], F1_Thresh_1[i])
    if verbose: print(string)
    print >>config.f, string
  string = "F1 Mean: %.4f\n"%np.mean(F1_1)
  if verbose: print(string)
  print >>config.f, string

  string = "###############################\n#######  Threshold 0.5 ########\n###############################\n"
  if verbose: print(string)
  print >>config.f, string

  if mode=='TEST':
    for i, au in enumerate(cfg.AUs):
      string = "---> [%s] AU%s F1: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_real5[i], F1_Thresh5[i])
      if verbose: print(string)
      print >>config.f, string
    string = "F1 Mean: %.4f\n"%np.mean(F1_real5)
    if verbose: print(string)
    print >>config.f, string

    for i, au in enumerate(cfg.AUs):
      string = "---> [%s] AU%s F1_median3: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_median3[i], F1_Thresh5[i])
      if verbose: print(string)
      print >>config.f, string
    string = "F1_median3 Mean: %.4f\n"%np.mean(F1_median3)
    if verbose: print(string)
    print >>config.f, string

    for i, au in enumerate(cfg.AUs):
      string = "---> [%s] AU%s F1_median5: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_median5[i], F1_Thresh5[i])
      if verbose: print(string)
      print >>config.f, string
    string = "F1_median5 Mean: %.4f\n"%np.mean(F1_median5)
    if verbose: print(string)
    print >>config.f, string

    for i, au in enumerate(cfg.AUs):
      string = "---> [%s] AU%s F1_median7: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_median7[i], F1_Thresh5[i])
      if verbose: print(string)
      print >>config.f, string
    string = "F1_median7 Mean: %.4f\n"%np.mean(F1_median7)
    if verbose: print(string)
    print >>config.f, string

  if mode=='TEST':
    string = "###############################\n######  Threshold VAL #######\n###############################\n"
    if verbose: print(string)
    print >>config.f, string 

  for i, au in enumerate(cfg.AUs):
    string = "---> [%s] AU%s F1: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_real[i], F1_Thresh_0[i])
    if verbose: print(string)
    print >>config.f, string
  string = "F1 Mean: %.4f\n"%np.mean(F1_real)
  if verbose: print(string)
  print >>config.f, string

  if mode=='TEST':
    for i, au in enumerate(cfg.AUs):
      string = "---> [%s] AU%s F1_median3: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_median3_th[i], F1_Thresh[i])
      if verbose: print(string)
      print >>config.f, string
    string = "F1_median3 Mean: %.4f\n"%np.mean(F1_median3_th)
    if verbose: print(string)
    print >>config.f, string

    for i, au in enumerate(cfg.AUs):
      string = "---> [%s] AU%s F1_median5: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_median5_th[i], F1_Thresh[i])
      if verbose: print(string)
      print >>config.f, string
    string = "F1_median5 Mean: %.4f\n"%np.mean(F1_median5_th)
    if verbose: print(string)
    print >>config.f, string

    for i, au in enumerate(cfg.AUs):
      string = "---> [%s] AU%s F1_median7: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_median7_th[i], F1_Thresh[i])
      if verbose: print(string)
      print >>config.f, string
    string = "F1_median7 Mean: %.4f\n"%np.mean(F1_median7_th)
    if verbose: print(string)
    print >>config.f, string

  string = "###############################\n#######  Threshold MAX ########\n###############################\n"
  if verbose: print(string)
  print >>config.f, string

  for i, au in enumerate(cfg.AUs):
    #REAL F1_MAX
    string = "---> [%s] AU%s F1_MAX: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_MAX[i], F1_Thresh_max[i])
    if verbose: print(string)
    print >>config.f, string
  string = "F1 Mean: %.4f\n"%np.mean(F1_MAX)
  if verbose: print(string)
  print >>config.f, string

  if mode=='VAL':
    return F1_real, F1_MAX, F1_Thresh_max, np.array(loss).mean(axis=0), F1_1
  else:
    return F1_real, F1_MAX, F1_Thresh_max  

##################################################################################################
##################################################################################################

def pdf2png(filename):
  from wand.image import Image
  from wand.color import Color
  with Image(filename="{}.pdf".format(filename), resolution=500) as img:
    with Image(width=img.width, height=img.height, background=Color("white")) as bg:
      bg.composite(img,0,0)
      bg.save(filename="{}.png".format(filename))
  os.remove('{}.pdf'.format(filename))