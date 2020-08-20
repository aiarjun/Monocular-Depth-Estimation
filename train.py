import time
import datetime
import pytz  
import wandb

import numpy as np
import torch 
import torch.nn as nn
import torch.nn.functional as F
import torchvision.utils as vutils
from torch.utils.tensorboard import SummaryWriter


from model.net import MonocularDepthModel, MonocularDepthModelWithUpconvolution  
from model.loss import Vgg16, combined_loss, mean_l2_loss
from model.metrics import evaluate_predictions
from model.dataloader import DataLoaders
from utils import *
from evaluate import evaluate

class Trainer():
  def __init__(self, data_path, resized = True):
    self.dataloaders = DataLoaders(data_path, resized = resized)  
    self.resized = resized

  def train_and_evaluate(self, config, checkpoint_file = '', local = False):
    batch_size = config['batch_size']
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

    train_dataloader = self.dataloaders.get_train_dataloader(batch_size = batch_size) 
    num_batches = len(train_dataloader)

    model = MonocularDepthModel()
    if self.resized == False:
      model = MonocularDepthModelWithUpconvolution(model)
    model = model.to(device)
    params = [param for param in model.parameters() if param.requires_grad == True]
    print('A total of %d parameters in present model' % (len(params)))
    optimizer = torch.optim.Adam(params, config['lr'])
    

    loss_model = Vgg16().to(device)

    best_rmse = 9e20
    is_best = False
    best_test_rmse = 9e20 
    
    if local:
      print('Loading checkpoint from local storage:',checkpoint_file)
      load_checkpoint(checkpoint_file, model, optimizer)
      print('Loaded checkpoint from local storage:',checkpoint_file)    

    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size = config['lr_scheduler_step_size'], gamma = 0.1)
    for i in range(config['start_epoch']):
      lr_scheduler.step() # step the scheduler for the already done epochs
    print('Training...')  
      
    wandb_step = config['start_epoch'] * num_batches -1 

    accumulated_per_pixel_loss = RunningAverage()
    accumulated_feature_loss = RunningAverage()
    accumulated_iteration_time = RunningAverage()
    for epoch in range(config['start_epoch'], config['epochs']):
      wandb_step += 1
      epoch_start_time = time.time()
      for iteration, batch in enumerate(train_dataloader):
        model.train() 
        time_start = time.time()        

        optimizer.zero_grad()
        images, depths = batch['img'], batch['depth']
        images = normalize_batch(torch.autograd.Variable(images.to(device)))
        depths = torch.autograd.Variable(depths.to(device))

        predictions = model(images)

        predictions_normalized = normalize_batch(predictions)
        depths_normalized = normalize_batch(depths)

        feature_losses_predictions = loss_model(predictions_normalized)
        feature_losses_depths = loss_model(depths_normalized)

        per_pixel_loss = combined_loss(predictions, depths)
        accumulated_per_pixel_loss.update(per_pixel_loss, images.shape[0])

        feature_loss = mean_l2_loss(feature_losses_predictions.relu2_2, feature_losses_depths.relu2_2)
        accumulated_feature_loss.update(feature_loss, images.shape[0])

        total_loss = per_pixel_loss + feature_loss
        total_loss.backward()
        optimizer.step()

        time_end = time.time()
        accumulated_iteration_time.update(time_end - time_start)
        eta = str(datetime.timedelta(seconds = int(accumulated_iteration_time() * (num_batches - iteration))))

        if iteration % config['training_loss_log_interval'] == 0: 
          print(datetime.datetime.now(pytz.timezone('Asia/Kolkata')), end = ' ')
          print('At epoch %d[%d/%d]; average per-pixel loss: %f; average feature loss' % (epoch, iteration, num_batches, accumulated_per_pixel_loss(), accumulated_feature_loss()))
          wandb.log({'Average per-pixel loss': accumulated_per_pixel_loss()}, step = wandb_step)
          wandb.log({'Average feature loss': accumulated_feature_loss()}, step = wandb_step)

        if iteration % config['other_metrics_log_interval'] == 0:

          print('Epoch: %d [%d / %d] ; it_time: %f (%f) ; eta: %s' % (epoch, iteration, num_batches, time_end - time_start, accumulated_iteration_time(), eta))
          metrics = evaluate_predictions(predictions, depths)
          self.write_metrics(metrics, wandb_step = wandb_step, train = True)
          test_images, test_depths, test_preds, test_loss, test_metrics = evaluate(model, self.dataloaders.get_val_dataloader, batch_size = config['test_batch_size'])
          self.compare_predictions(test_images, test_depths, test_preds, wandb_step)	
          wandb.log({'Average Validation loss on random batch':test_loss.item()}, step = wandb_step)	
          self.write_metrics(test_metrics, wandb_step = wandb_step, train = False)

          if metrics['rmse'] < best_rmse: 
            wandb.run.summary["best_train_rmse"] = metrics['rmse']
            best_rmse = metrics['rmse']
            is_best = True

          save_checkpoint({'iteration': wandb_step, 
                          'state_dict': model.state_dict(), 
                          'optim_dict': optimizer.state_dict()},
                          is_best = is_best,
                          checkpoint_dir = 'experiments/', train = True)

          if test_metrics['rmse'] < best_test_rmse:
            wandb.run.summary["best_test_rmse"] = test_metrics['rmse'] 
            best_test_rmse = test_metrics['rmse']

          is_best = False
 
                               
      epoch_end_time = time.time()
      print('Epoch %d complete, time taken: %s' % (epoch, str(datetime.timedelta(seconds = int(epoch_end_time - epoch_start_time)))))
      lr_scheduler.step() 
      torch.cuda.empty_cache()

      save_epoch({'state_dict': model.state_dict(), 	
                  'optim_dict': optimizer.state_dict()}, epoch_index = epoch)

  def write_metrics(self, metrics, wandb_step, train = True):	
    if train:	
      for key, value in metrics.items():	
        wandb.log({'Train '+key: value}, step = wandb_step)	
    else:	
      for key, value in metrics.items():	
        wandb.log({'Validation '+key: value}, step = wandb_step) 	

  def compare_predictions(self, images, depths, predictions, wandb_step):	
    image_plots = plot_batch_images(images)	
    depth_plots = plot_batch_depths(depths)	
    pred_plots = plot_batch_depths(predictions)	
    difference = plot_batch_depths(torch.abs(depths - predictions))	

    wandb.log({"Sample Validation images": [wandb.Image(image_plot) for image_plot in image_plots]}, step = wandb_step)	
    wandb.log({"Sample Validation depths": [wandb.Image(image_plot) for image_plot in depth_plots]}, step = wandb_step)	
    wandb.log({"Sample Validation predictions": [wandb.Image(image_plot) for image_plot in pred_plots]}, step = wandb_step)	
    wandb.log({"Sample Validation differences": [wandb.Image(image_plot) for image_plot in difference]}, step = wandb_step)	

    del image_plots	
    del depth_plots	
    del pred_plots	
    del difference