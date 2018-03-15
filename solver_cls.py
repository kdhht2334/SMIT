import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import os
import time
import sys
import datetime
from torch.autograd import grad
from torch.autograd import Variable
from torchvision.utils import save_image
from torchvision import transforms

from PIL import Image
import ipdb
import config as cfg
import glob
import pickle
from utils import f1_score, f1_score_max

# CUDA_VISIBLE_DEVICES=3 ipython main.py -- --c_dim=12 --num_epochs 4  --dataset MultiLabelAU --batch_size 32 --image_size 256 --d_repeat_num 7 --g_repeat_num 7 --multi_binary --au 1 --au_model aunet
# CUDA_VISIBLE_DEVICES=0 ipython main.py -- --c_dim=12 --num_epochs 10  --dataset MultiLabelAU --batch_size 8 --image_size 256 --d_repeat_num 7 --g_repeat_num 7 --fold 1
# CUDA_VISIBLE_DEVICES=1 ipython main.py -- --c_dim=12 --num_epochs 10  --dataset MultiLabelAU --batch_size 8 --image_size 256 --d_repeat_num 7 --g_repeat_num 7 --fold 2


class Solver(object):

    def __init__(self, MultiLabelAU_loader, config, CelebA=None):
        # Data loader
        self.MultiLabelAU_loader = MultiLabelAU_loader
        self.CelebA_loader = CelebA

        # Model hyper-parameters
        self.c_dim = config.c_dim
        self.c2_dim = config.c2_dim
        self.image_size = config.image_size
        self.g_conv_dim = config.g_conv_dim
        self.d_conv_dim = config.d_conv_dim
        self.g_repeat_num = config.g_repeat_num
        self.d_repeat_num = config.d_repeat_num
        self.d_train_repeat = config.d_train_repeat

        # Hyper-parameteres
        self.lambda_cls = config.lambda_cls
        self.lambda_rec = config.lambda_rec
        self.lambda_gp = config.lambda_gp
        self.g_lr = config.g_lr
        self.d_lr = config.d_lr
        self.beta1 = config.beta1
        self.beta2 = config.beta2

        # Training settings
        self.dataset = config.dataset
        self.num_epochs = config.num_epochs
        self.num_epochs_decay = config.num_epochs_decay
        self.num_iters = config.num_iters
        self.num_iters_decay = config.num_iters_decay
        self.batch_size = config.batch_size
        self.use_tensorboard = config.use_tensorboard
        self.pretrained_model = config.pretrained_model

        self.FOCAL_LOSS = config.FOCAL_LOSS
        self.JUST_REAL = config.JUST_REAL
        self.FAKE_CLS = config.FAKE_CLS
        self.DENSENET = config.DENSENET     
        self.DYNAMIC_COLOR = config.DYNAMIC_COLOR  
        self.GOOGLE = config.GOOGLE    

        #Training Binary Classifier Settings
        # self.au_model = config.au_model
        # self.au = config.au
        # self.multi_binary = config.multi_binary
        # self.pretrained_model_generator = config.pretrained_model_generator
        # self.pretrained_model_discriminator = config.pretrained_model_discriminator

        # Test settings
        self.test_model = config.test_model
        self.metadata_path = config.metadata_path

        # Path
        self.log_path = config.log_path
        self.sample_path = config.sample_path
        self.model_save_path = config.model_save_path
        self.result_path = config.result_path
        self.fold = config.fold
        self.mode_data = config.mode_data

        # Step size
        self.log_step = config.log_step
        self.sample_step = config.sample_step
        self.model_save_step = config.model_save_step

        # Build tensorboard if use
        self.build_model()
        if self.use_tensorboard:
            self.build_tensorboard()

        # Start with trained model
        if self.pretrained_model:
            self.load_pretrained_model()

    def build_model(self):
        # Define a generator and a discriminator

        from model import Generator
        if not self.JUST_REAL: self.G = Generator(self.g_conv_dim, self.c_dim, self.g_repeat_num)

        if self.DENSENET:
            from models.densenet import Generator, densenet121 as Discriminator
            self.D = Discriminator(num_classes = self.c_dim) 
        else:
            from model import Discriminator
            self.D = Discriminator(self.image_size, self.d_conv_dim, self.c_dim, self.d_repeat_num) 


        # Optimizers
        self.d_optimizer = torch.optim.Adam(self.D.parameters(), self.d_lr, [self.beta1, self.beta2])

        # Print networks
        if not self.JUST_REAL:self.print_network(self.G, 'G')
        self.print_network(self.D, 'D')

        if torch.cuda.is_available():
            if not self.JUST_REAL: self.G.cuda()
            self.D.cuda()

    def print_network(self, model, name):
        num_params = 0
        for p in model.parameters():
            num_params += p.numel()
        print(name)
        print(model)
        print("The number of parameters: {}".format(num_params))

    def load_pretrained_model(self):
        model = os.path.join(
            self.model_save_path, '{}_D.pth'.format(self.pretrained_model))
        self.D.load_state_dict(torch.load(model))
        print('loaded CLS trained model: {}!'.format(model))

    def build_tensorboard(self):
        from logger import Logger
        self.logger = Logger(self.log_path)

    def update_lr(self, d_lr):
        for param_group in self.d_optimizer.param_groups:
            param_group['lr'] = d_lr

    def reset_grad(self):
        self.d_optimizer.zero_grad()

    def to_var(self, x, volatile=False):
        if torch.cuda.is_available():
            x = x.cuda()
        return Variable(x, volatile=volatile)

    def denorm(self, x):
        out = (x + 1) / 2
        return out.clamp_(0, 1)

    def threshold(self, x):
        x = x.clone()
        x = (x >= 0.5).float()
        return x

    def color_up(self, labels):
        where_pos = lambda x,y: np.where(x.data.cpu().numpy().flatten()==y)[0]
        color_up = 0.2
        rgb = np.zeros((self.batch_size, 3, 224, 224)).astype(np.float32)
        green_r_pos = where_pos(labels,1)
        rgb[green_r_pos,1,:,:] += color_up
        red_r_pos = where_pos(labels,0)
        rgb[red_r_pos,0,:,:] += color_up   
        rgb = Variable(torch.FloatTensor(rgb))
        if torch.cuda.is_available(): rgb = rgb.cuda()
        return rgb

    def compute_accuracy(self, x, y, dataset):
        if dataset == 'CelebA' or dataset=='MultiLabelAU':
            x = F.sigmoid(x)
            predicted = self.threshold(x)
            correct = (predicted == y).float()
            accuracy = torch.mean(correct, dim=0) * 100.0
        else:
            _, predicted = torch.max(x, dim=1)
            correct = (predicted == y).float()
            accuracy = torch.mean(correct) * 100.0
        return accuracy

    def one_hot(self, labels, dim):
        """Convert label indices to one-hot vector"""
        batch_size = labels.size(0)
        out = torch.zeros(batch_size, dim)
        # ipdb.set_trace()
        out[np.arange(batch_size), labels.long()] = 1
        return out

    def show_img(self, img, real_label, fake_label):                  
        import matplotlib.pyplot as plt
        fake_image_list=[img]

        for fl in fake_label:
            # ipdb.set_trace()
            fake_image_list.append(self.G(img, self.to_var(fl.data, volatile=True)))
        fake_images = torch.cat(fake_image_list, dim=3)        
        shape0 = min(8, fake_images.data.cpu().shape[0])
        # ipdb.set_trace()
        save_image(self.denorm(fake_images.data.cpu()[:shape0]), 'tmp_all.jpg',nrow=1, padding=0)
        print("Real Label: \n"+str(real_label.data.cpu()[:shape0].numpy()))
        for fl in fake_label:
            print("Fake Label: \n"+str(fl.data.cpu()[:shape0].numpy()))        
        os.system('eog tmp_all.jpg')        
        os.remove('tmp_all.jpg')

    def train(self):
        """Train StarGAN within a single dataset."""

        # Set dataloader
        if self.dataset == 'MultiLabelAU':
            self.data_loader = self.MultiLabelAU_loader            
        elif self.dataset == 'au01_fold0':
            self.data_loader = self.au_loader      

        # The number of iterations per epoch
        iters_per_epoch = len(self.data_loader)

        if not self.JUST_REAL:
        
            print(" [!] Loading Generator from "+self.pretrained_model_generator)
            self.G.load_state_dict(torch.load(self.pretrained_model_generator))

            try:
                if not self.pretrained_model:
                    self.D.load_state_dict(torch.load(self.pretrained_model_discriminator))
                    print(" [!] Loading Discriminator from "+self.pretrained_model_discriminator)
            except:
                pass

        # lr cache for decaying
        g_lr = self.g_lr
        d_lr = self.d_lr

        # Start with trained model if exists
        if self.pretrained_model:
            start = int(self.pretrained_model.split('_')[0])
            # Decay learning rate
            for i in range(start):
                if (i+1) > (self.num_epochs - self.num_epochs_decay):
                    # g_lr -= (self.g_lr / float(self.num_epochs_decay))
                    d_lr -= (self.d_lr / float(self.num_epochs_decay))
                    self.update_lr(d_lr)
                    print ('Decay learning rate to d_lr: {}.'.format(d_lr))            
        else:
            start = 0

        # Start training
        fake_loss_cls = None
        real_loss_cls = None
        log_real = 'N/A'
        log_fake = 'N/A'
        fake_iters = 1
        fake_rate = 1
        real_rate = 1
        start_time = time.time()

        last_model_step = len(self.data_loader)

        for i, (real_x, real_label, files) in enumerate(self.data_loader): break
        real_x = self.to_var(real_x, volatile=True)
        real_label = self.to_var(real_label, volatile=True)
        fake_list = []
        fake_c=real_label.clone()*0
        fake_list.append(fake_c.clone())
        for i in range(12):
            fake_c[:,i]=1
            fake_list.append(fake_c.clone())

        # ipdb.set_trace()        
        # self.show_img(real_x, real_label, fake_list)
        # ipdb.set_trace()

        for e in range(start, self.num_epochs):
            E = str(e+1).zfill(2)
            for i, (real_x, real_label, files) in enumerate(self.data_loader):

                if self.DYNAMIC_COLOR:
                    random_brightness = (torch.rand( real_x.size(0), 1, 1, 1 )-0.5)/3. #-42 to 42
                    real_x = torch.add(real_x, random_brightness).clamp(min=-1, max=1)
                
                real_x_var = self.to_var(real_x)

                if (i+e)%2==0 or self.JUST_REAL:
                    real_c = real_label.clone()
                    # Convert tensor to variable
                    real_label_var = self.to_var(real_label)   # this is same as real_c if dataset == 'CelebA'

                    _, real_out_cls = self.D(real_x_var)

                    real_loss_cls = F.binary_cross_entropy_with_logits(
                        real_out_cls, real_label_var, size_average=False) / real_x_var.size(0)

                    self.reset_grad()
                    real_loss_cls.backward()
                    self.d_optimizer.step()

                #if e>=1 and i%fake_rate:
                if (i+e+1)%2==0 and not self.JUST_REAL:
                    # ================== Train C FAKE ================== #

                    # # Compute loss with fake images
                    for _ in range(fake_iters):
                        # ipdb.set_trace()
                        real_label_ = real_label.clone()
                        luck = np.random.randint(0,2)
                        if luck:
                            rand_idx = torch.randperm(real_label_.size(0))
                            fake_label = real_label_[rand_idx]
                        else:
                            fake_label = torch.from_numpy(np.random.randint(0,2,[real_label_.size(0),real_label_.size(1)]).astype(np.float32))
                            if torch.cuda.is_available():
                                fake_label = fake_label.cuda()

                        # ipdb.set_trace()

                        fake_c = fake_label.clone()
                        fake_c_var = self.to_var(fake_c)                   
                        try:
                            fake_x = self.G(real_x_var, fake_c_var)
                        except: 
                            ipdb.set_trace()
                        fake_x_var = Variable(fake_x.data)
                        _, fake_out_cls = self.D(fake_x_var)


                        # fake_label = torch.clamp(torch.add(real_label, fake_label), max=1)##TRICK
                        fake_label_var = self.to_var(fake_label)
                        fake_loss_cls = F.binary_cross_entropy_with_logits(
                               fake_out_cls, fake_label_var, size_average=False) / fake_x.size(0)

                        # # Backward + Optimize
                        self.reset_grad()
                        fake_loss_cls.backward()
                        self.d_optimizer.step()

                # ipdb.set_trace()

                # ================== LOG ================== #
                # Compute classification accuracy of the classifier
                if (i+1) % self.log_step == 0 or (i+1)==last_model_step:
                    if real_loss_cls is not None:
                        accuracies = self.compute_accuracy(real_out_cls, real_label_var, self.dataset)
                        log_real = ["{:.2f}".format(acc) for acc in accuracies.data.cpu().numpy()]
                    if fake_loss_cls is not None:
                        accuracies = self.compute_accuracy(fake_out_cls, fake_label_var, self.dataset)
                        log_fake = ["{:.2f}".format(acc) for acc in accuracies.data.cpu().numpy()]
                    # ipdb.set_trace()
                    print('Classification Acc (AU): \n Real: %s \n Fake: %s'%(log_real, log_fake))


                # Logging
                loss = {}
                if real_loss_cls is not None: loss['real_loss_cls'] = real_loss_cls.data[0]
                if fake_loss_cls is not None: loss['fake_loss_cls'] = fake_loss_cls.data[0]

                # Print out log info
                if (i+1) % self.log_step == 0 or (i+1)==last_model_step:
                    elapsed = time.time() - start_time
                    elapsed = str(datetime.timedelta(seconds=elapsed))

                    log = "Elapsed [{}], Epoch [{}/{}], Iter [{}/{}] [!CLS] [fold{}] [{}]".format(
                        elapsed, E, self.num_epochs, i+1, iters_per_epoch, self.fold, self.image_size)                    


                    if self.FOCAL_LOSS: log += ' [*FL]'
                    if self.DENSENET: log += ' [*DENSENET]'
                    # if self.FAKE_CLS: log += ' [*FAKE_CLS]'
                    if self.JUST_REAL: log += ' [*JUST_REAL]'


                    for tag, value in loss.items():
                        log += ", {}: {:.4f}".format(tag, value)
                    print(log)

                    if self.use_tensorboard:
                        print("Log path: "+self.log_path)
                        for tag, value in loss.items():
                            self.logger.scalar_summary(tag, value, e * iters_per_epoch + i + 1)

                # Save model checkpoints
                if (i+1) % self.model_save_step == 0 or (i+1)==last_model_step:
                    # torch.save(self.G.state_dict(),
                    #     os.path.join(self.model_save_path, '{}_{}_G.pth'.format(e+1, i+1)))
                    torch.save(self.D.state_dict(),
                        os.path.join(self.model_save_path, '{}_{}_D.pth'.format(E, i+1)))

            # Decay learning rate
            if (e+1) > (self.num_epochs - self.num_epochs_decay):
                # g_lr -= (self.g_lr / float(self.num_epochs_decay))
                d_lr -= (self.d_lr / float(self.num_epochs_decay))
                self.update_lr(d_lr)
                print ('Decay learning rate to d_lr: {}.'.format(d_lr))


    def train_multi(self):
        """Train StarGAN within a single dataset."""

        # The number of iterations per epoch
        iters_per_epoch = len(self.MultiLabelAU_loader)

        if not self.JUST_REAL:
        
            print(" [!] Loading Generator from "+self.pretrained_model_generator)
            self.G.load_state_dict(torch.load(self.pretrained_model_generator))

            try:
                if not self.pretrained_model:
                    self.D.load_state_dict(torch.load(self.pretrained_model_discriminator))
                    print(" [!] Loading Discriminator from "+self.pretrained_model_discriminator)
            except:
                pass

        # lr cache for decaying
        g_lr = self.g_lr
        d_lr = self.d_lr

        # Start with trained model if exists
        if self.pretrained_model:
            start = int(self.pretrained_model.split('_')[0])
            # Decay learning rate
            for i in range(start):
                if (i+1) > (self.num_epochs - self.num_epochs_decay):
                    # g_lr -= (self.g_lr / float(self.num_epochs_decay))
                    d_lr -= (self.d_lr / float(self.num_epochs_decay))
                    self.update_lr(d_lr)
                    print ('Decay learning rate to d_lr: {}.'.format(d_lr))            
        else:
            start = 0

        # Start training
        fake_loss_cls = None
        real_loss_cls = None
        log_real = 'N/A'
        log_fake = 'N/A'
        fake_iters = 1
        fake_rate = 1
        real_rate = 1
        start_time = time.time()

        last_model_step = len(self.MultiLabelAU_loader)

        for i, (real_x, real_label, files) in enumerate(self.MultiLabelAU_loader): break
        real_x = self.to_var(real_x, volatile=True)
        real_label = self.to_var(real_label, volatile=True)
        fake_list = []
        fake_c=real_label.clone()*0
        fake_list.append(fake_c.clone())
        for i in range(12):
            fake_c[:,i]=1
            fake_list.append(fake_c.clone())

        # ipdb.set_trace()        
        # self.show_img(real_x, real_label, fake_list)
        # ipdb.set_trace()

        data_iter2 = iter(self.CelebA_loader)

        for e in range(start, self.num_epochs):
            E = str(e+1).zfill(2)
            for i, (real_x, real_label, files) in enumerate(self.MultiLabelAU_loader):

                if self.DYNAMIC_COLOR:
                    random_brightness = (torch.rand( real_x.size(0), 1, 1, 1 )-0.5)/3. #-42 to 42
                    real_x = torch.add(real_x, random_brightness).clamp(min=-1, max=1)
                
                real_x_var = self.to_var(real_x)

                if (i+e)%2==0 or self.JUST_REAL:
                    real_c = real_label.clone()
                    # Convert tensor to variable
                    real_label_var = self.to_var(real_label)   # this is same as real_c if dataset == 'CelebA'

                    _, real_out_cls = self.D(real_x_var)

                    real_loss_cls = F.binary_cross_entropy_with_logits(
                        real_out_cls, real_label_var, size_average=False) / real_x_var.size(0)

                    self.reset_grad()
                    real_loss_cls.backward()
                    self.d_optimizer.step()

                #if e>=1 and i%fake_rate:
                if (i+e+1)%2==0 and not self.JUST_REAL:
                    # ================== Train C FAKE ================== #

                    # # Compute loss with fake images
                    for _ in range(fake_iters):
                        # ipdb.set_trace()

                        #################CelebA#################
                        try:
                            real_x2, real_label2, _ = next(data_iter2)
                        except:
                            # ipdb.set_trace()
                            data_iter2 = iter(self.CelebA_loader)
                            real_x2, real_label2, _ = next(data_iter2)

                        real_x_var = self.to_var(real_x2)
                        ########################################                        

                        # real_label_ = real_label.clone()
                        real_label_ = real_label.clone()
                        # luck = np.random.randint(0,2)
                        # if luck:
                        #     rand_idx = torch.randperm(real_label_.size(0))
                        #     fake_label = real_label_[rand_idx]
                        # else:
                        fake_label = torch.from_numpy(np.random.randint(0,2,[real_x2.size(0),real_label.size(1)]).astype(np.float32))

                        # ipdb.set_trace()
                        fake_c_var = self.to_var(fake_label)                   
                        try:
                            fake_x = self.G(real_x_var, fake_c_var)
                        except: 
                            ipdb.set_trace()
                        fake_x_var = Variable(fake_x.data)
                        _, fake_out_cls = self.D(fake_x_var)


                        # fake_label = torch.clamp(torch.add(real_label, fake_label), max=1)##TRICK
                        fake_label_var = self.to_var(fake_label)
                        fake_loss_cls = F.binary_cross_entropy_with_logits(
                               fake_out_cls, fake_label_var, size_average=False) / fake_x.size(0)

                        # # Backward + Optimize
                        self.reset_grad()
                        fake_loss_cls.backward()
                        self.d_optimizer.step()

                # ipdb.set_trace()

                # ================== LOG ================== #
                # Compute classification accuracy of the classifier
                if (i+1) % self.log_step == 0 or (i+1)==last_model_step:
                    if real_loss_cls is not None:
                        accuracies = self.compute_accuracy(real_out_cls, real_label_var, self.dataset)
                        log_real = ["{:.2f}".format(acc) for acc in accuracies.data.cpu().numpy()]
                    if fake_loss_cls is not None:
                        accuracies = self.compute_accuracy(fake_out_cls, fake_label_var, self.dataset)
                        log_fake = ["{:.2f}".format(acc) for acc in accuracies.data.cpu().numpy()]
                    # ipdb.set_trace()
                    print('Classification Acc (AU): \n Real: %s \n Fake: %s'%(log_real, log_fake))


                # Logging
                loss = {}
                if real_loss_cls is not None: loss['real_loss_cls'] = real_loss_cls.data[0]
                if fake_loss_cls is not None: loss['fake_loss_cls'] = fake_loss_cls.data[0]

                # Print out log info
                if (i+1) % self.log_step == 0 or (i+1)==last_model_step:
                    elapsed = time.time() - start_time
                    elapsed = str(datetime.timedelta(seconds=elapsed))

                    log = "Elapsed [{}], Epoch [{}/{}], Iter [{}/{}] [!CLS] [fold{}] [{}]".format(
                        elapsed, E, self.num_epochs, i+1, iters_per_epoch, self.fold, self.image_size)                    


                    if self.FOCAL_LOSS: log += ' [*FL]'
                    if self.DENSENET: log += ' [*DENSENET]'
                    # if self.FAKE_CLS: log += ' [*FAKE_CLS]'
                    if self.JUST_REAL: log += ' [*JUST_REAL]'


                    for tag, value in loss.items():
                        log += ", {}: {:.4f}".format(tag, value)
                    print(log)

                    if self.use_tensorboard:
                        print("Log path: "+self.log_path)
                        for tag, value in loss.items():
                            self.logger.scalar_summary(tag, value, e * iters_per_epoch + i + 1)

                # Save model checkpoints
                if (i+1) % self.model_save_step == 0 or (i+1)==last_model_step:
                    # torch.save(self.G.state_dict(),
                    #     os.path.join(self.model_save_path, '{}_{}_G.pth'.format(e+1, i+1)))
                    torch.save(self.D.state_dict(),
                        os.path.join(self.model_save_path, '{}_{}_D.pth'.format(E, i+1)))

            # Decay learning rate
            if (e+1) > (self.num_epochs - self.num_epochs_decay):
                # g_lr -= (self.g_lr / float(self.num_epochs_decay))
                d_lr -= (self.d_lr / float(self.num_epochs_decay))
                self.update_lr(d_lr)
                print ('Decay learning rate to d_lr: {}.'.format(d_lr))


    def test_cls(self):
        """Facial attribute transfer on CelebA or facial expression synthesis on RaFD."""
        # Load trained parameters
        from data_loader import get_loader
        if self.test_model=='':
            last_file = sorted(glob.glob(os.path.join(self.model_save_path,  '*_D.pth')))[-1]
            last_name = '_'.join(last_file.split('/')[-1].split('_')[:2])
        else:
            last_name = self.test_model

        # G_path = os.path.join(self.model_save_path, '{}_G.pth'.format(last_name))
        D_path = os.path.join(self.model_save_path, '{}_D.pth'.format(last_name))
        txt_path = os.path.join(self.model_save_path, '{}_{}.txt'.format(last_name,'{}'))
        self.pkl_data = os.path.join(self.model_save_path, '{}_{}.pkl'.format(last_name, '{}'))
        self.lstm_path = os.path.join(self.model_save_path, '{}_lstm'.format(last_name))
        if not os.path.isdir(self.lstm_path): os.makedirs(self.lstm_path)
        print(" [!!] {} model loaded...".format(D_path))
        # self.G.load_state_dict(torch.load(G_path))
        self.D.load_state_dict(torch.load(D_path))
        # self.G.eval()
        self.D.eval()
        # ipdb.set_trace()
        if self.dataset == 'MultiLabelAU':
            data_loader_train = get_loader(self.metadata_path, self.image_size,
                                   self.image_size, self.batch_size, 'MultiLabelAU', 'train', no_flipping = True)
            data_loader_test = get_loader(self.metadata_path, self.image_size,
                                   self.image_size, self.batch_size, 'MultiLabelAU', 'test')
        elif dataset == 'au01_fold0':
            data_loader = self.au_loader    


        if not hasattr(self, 'output_txt'):
            # ipdb.set_trace()
            self.output_txt = txt_path
            try:
                self.output_txt  = sorted(glob.glob(self.output_txt.format('*')))[-1]
                number_file = len(glob.glob(self.output_txt))
            except:
                number_file = 0
            self.output_txt = self.output_txt.format(str(number_file).zfill(2)) 
        
        self.f=open(self.output_txt, 'a')     
        self.thresh = np.linspace(0.01,0.99,200).astype(np.float32)
        # ipdb.set_trace()
        F1_real, F1_max, max_thresh_train  = self.F1_TEST(data_loader_train, mode = 'TRAIN')
        _ = self.F1_TEST(data_loader_test, thresh = max_thresh_train)
     
        self.f.close()

    def F1_TEST(self, data_loader, mode = 'TEST', thresh = [0.5]*len(cfg.AUs)):

        PREDICTION = []
        GROUNDTRUTH = []
        total_idx=int(len(data_loader)/self.batch_size)  
        count = 0
        for i, (real_x, org_c, files) in enumerate(data_loader):
            if os.path.isfile(self.pkl_data.format(mode.lower())): 
                PREDICTION, GROUNDTRUTH = pickle.load(open(self.pkl_data.format(mode.lower())))
                break
            # ipdb.set_trace()
            real_x = self.to_var(real_x, volatile=True)
            labels = org_c
            
            
            # ipdb.set_trace()
            _, out_cls_temp, lstm_input = self.D(real_x, lstm=True)
            self.save_lstm(lstm_input.data.cpu().numpy(), files)
            # output = ((F.sigmoid(out_cls_temp)>=0.5)*1.).data.cpu().numpy()
            output = F.sigmoid(out_cls_temp)
            if i==0:
                print(mode.upper())
                print("Predicted:   "+str((output>=0.5)*1))
                print("Groundtruth: "+str(org_c))

            count += org_c.shape[0]
            string_ = str(count)+' / '+str(len(data_loader)*self.batch_size)
            sys.stdout.write("\r%s" % string_)
            sys.stdout.flush()        
            # ipdb.set_trace()

            PREDICTION.append(output.data.cpu().numpy().tolist())
            GROUNDTRUTH.append(labels.cpu().numpy().astype(np.uint8).tolist())

        if not os.path.isfile(self.pkl_data.format(mode.lower())): 
            pickle.dump([PREDICTION, GROUNDTRUTH], open(self.pkl_data.format(mode.lower()), 'w'))
        print("")
        print >>self.f, ""
        # print("[Min and Max predicted: "+str(min(prediction))+ " " + str(max(prediction))+"]")
        # print >>self.f, "[Min and Max predicted: "+str(min(prediction))+ " " + str(max(prediction))+"]"
        print("")

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
                _, F1_real[i], F1_Thresh[i], F1_median3_th[i], F1_median5_th[i], F1_median7_th[i] = f1_score(np.array(groundtruth), np.array(prediction), thresh[i], median=True)
            else:
                _, F1_real[i], F1_Thresh[i] = f1_score(np.array(groundtruth), np.array(prediction), thresh[i])
            _, F1_0[i], F1_Thresh_0[i] = f1_score(np.array(groundtruth), np.array(prediction)*0, thresh[i])
            _, F1_1[i], F1_Thresh_1[i] = f1_score(np.array(groundtruth), (np.array(prediction)*0)+1, thresh[i])
            _, F1_MAX[i], F1_Thresh_max[i] = f1_score_max(np.array(groundtruth), np.array(prediction), self.thresh)     


        for i, au in enumerate(cfg.AUs):
            string = "---> [%s - 0] AU%s F1: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_0[i], F1_Thresh_0[i])
            print(string)
            print >>self.f, string
        string = "F1 Mean: %.4f"%np.mean(F1_0)
        print(string)
        print("")
        print >>self.f, string
        print >>self.f, ""

        for i, au in enumerate(cfg.AUs):
            string = "---> [%s - 1] AU%s F1: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_1[i], F1_Thresh_1[i])
            print(string)
            print >>self.f, string
        string = "F1 Mean: %.4f"%np.mean(F1_1)
        print(string)
        print("")
        print >>self.f, string
        print >>self.f, ""

        string = "###############################\n#######  Threshold 0.5 ########\n###############################"
        print(string)
        print("")
        print >>self.f, string
        print >>self.f, ""  

        if mode=='TEST':
            for i, au in enumerate(cfg.AUs):
                string = "---> [%s] AU%s F1: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_real5[i], F1_Thresh5[i])
                print(string)
                print >>self.f, string
            string = "F1 Mean: %.4f"%np.mean(F1_real5)
            print(string)
            print("")
            print >>self.f, string
            print >>self.f, ""

            for i, au in enumerate(cfg.AUs):
                string = "---> [%s] AU%s F1_median3: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_median3[i], F1_Thresh5[i])
                print(string)
                print >>self.f, string
            string = "F1_median3 Mean: %.4f"%np.mean(F1_median3)
            print(string)
            print("")
            print >>self.f, string
            print >>self.f, ""

            for i, au in enumerate(cfg.AUs):
                string = "---> [%s] AU%s F1_median5: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_median5[i], F1_Thresh5[i])
                print(string)
                print >>self.f, string
            string = "F1_median5 Mean: %.4f"%np.mean(F1_median5)
            print(string)
            print("")
            print >>self.f, string
            print >>self.f, ""

            for i, au in enumerate(cfg.AUs):
                string = "---> [%s] AU%s F1_median7: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_median7[i], F1_Thresh5[i])
                print(string)
                print >>self.f, string
            string = "F1_median7 Mean: %.4f"%np.mean(F1_median7)
            print(string)
            print("")
            print >>self.f, string
            print >>self.f, ""            

        if mode=='TEST':
            string = "###############################\n######  Threshold Train #######\n###############################"
            print(string)
            print("")
            print >>self.f, string
            print >>self.f, ""    

        for i, au in enumerate(cfg.AUs):
            string = "---> [%s] AU%s F1: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_real[i], F1_Thresh_0[i])
            print(string)
            print >>self.f, string
        string = "F1 Mean: %.4f"%np.mean(F1_real)
        print(string)
        print("")
        print >>self.f, string
        print >>self.f, ""

        if mode=='TEST':
            for i, au in enumerate(cfg.AUs):
                string = "---> [%s] AU%s F1_median3: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_median3_th[i], F1_Thresh[i])
                print(string)
                print >>self.f, string
            string = "F1_median3 Mean: %.4f"%np.mean(F1_median3_th)
            print(string)
            print("")
            print >>self.f, string
            print >>self.f, ""

            for i, au in enumerate(cfg.AUs):
                string = "---> [%s] AU%s F1_median5: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_median5_th[i], F1_Thresh[i])
                print(string)
                print >>self.f, string
            string = "F1_median5 Mean: %.4f"%np.mean(F1_median5_th)
            print(string)
            print("")
            print >>self.f, string
            print >>self.f, ""

            for i, au in enumerate(cfg.AUs):
                string = "---> [%s] AU%s F1_median7: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_median7_th[i], F1_Thresh[i])
                print(string)
                print >>self.f, string
            string = "F1_median7 Mean: %.4f"%np.mean(F1_median7_th)
            print(string)
            print("")
            print >>self.f, string
            print >>self.f, ""                

        string = "###############################\n#######  Threshold MAX ########\n###############################"
        print(string)
        print("")
        print >>self.f, string
        print >>self.f, ""  

        for i, au in enumerate(cfg.AUs):
            #REAL F1_MAX
            string = "---> [%s] AU%s F1_MAX: %.4f, Threshold: %.4f <---" % (mode, str(au).zfill(2), F1_MAX[i], F1_Thresh_max[i])
            print(string)
            print >>self.f, string
        string = "F1 Mean: %.4f"%np.mean(F1_MAX)
        print(string)
        print("")
        print >>self.f, string
        print >>self.f, ""

        return F1_real, F1_MAX, F1_Thresh_max     

    def save_lstm(self, data, files):
        assert data.shape[0]==len(files)
        for i in range(len(files)):
            name = os.path.join(self.lstm_path, '/'.join(files[i].split('/')[-6:]))
            name = name.replace('jpg', 'npy')
            folder = os.path.dirname(name)
            if not os.path.isdir(folder): os.makedirs(folder)
            np.save(name, data[i])
