import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torchvision.transforms as transforms
import tqdm
import os
import numpy as np
import time

from misc_utils import get_aucroc
from storage_utils import load_best_model_state_dict, save_statistics


class ExperimentBuilder(nn.Module):
    def __init__(self, network_model, train_data, val_data,
                 test_data, device, args):
        """
        Initializes an ExperimentBuilder object. Such an object takes care of running training and evaluation of a deep net
        on a given dataset. It also takes care of saving per epoch models and automatically inferring the best val model
        to be used for evaluating the test set metrics.
        :param network_model: A pytorch nn.Module which implements a network architecture.
        :param experiment_name: The name of the experiment. This is used mainly for keeping track of the experiment and creating and directory structure that will be used to save logs, model parameters and other.
        :param num_epochs: Total number of epochs to run the experiment
        :param train_data: An object of the DataProvider type. Contains the training set.
        :param val_data: An object of the DataProvider type. Contains the val set.
        :param test_data: An object of the DataProvider type. Contains the test set.
        :param weight_decay_coefficient: A float indicating the weight decay to use with the adam optimizer.
        :param use_gpu: A boolean indicating whether to use a GPU or not.
        :param continue_from_epoch: An int indicating whether we'll start from scrach (-1) or whether we'll reload a previously saved model of epoch 'continue_from_epoch' and continue training from there.
        """
        super(ExperimentBuilder, self).__init__()

        self.experiment_name = args.experiment_name
        self.model = network_model
        self.model.reset_parameters()
        self.device = device

        if torch.cuda.device_count() > 1:
            self.model.to(self.device)
            self.model = nn.DataParallel(module=self.model)
        else:
            self.model.to(self.device)  # sends the model from the cpu to the gpu
          # re-initialize network parameters
        self.train_data = train_data
        self.val_data = val_data
        self.test_data = test_data
        self.optimizer = optim.Adam(self.parameters(), amsgrad=False, lr=args.learning_rate, betas=args.betas,
                                    weight_decay=args.weight_decay_coefficient)
        self.task = args.task
        self.loss = args.loss
        # Generate the directory names
        self.experiment_folder = os.path.abspath(os.path.join("results", self.experiment_name))
        self.experiment_logs = os.path.abspath(os.path.join(self.experiment_folder, "result_outputs"))
        self.experiment_saved_models = os.path.abspath(os.path.join(self.experiment_folder, "saved_models"))
        print(self.experiment_folder, self.experiment_logs)
        # Set best models to be at 0 since we are just starting
        self.best_val_model_idx = 0
        
        if self.task == "classification":
            self.best_val_model_measure = 1000000000 # performance measure for choosing best epoch: loss
        elif self.task == "regression":
            self.best_val_model_measure = 1000000000 # performance measure for choosing best epoch: loss

        if not os.path.exists(self.experiment_folder):  # If experiment directory does not exist
            os.mkdir(self.experiment_folder)  # create the experiment directory

        if not os.path.exists(self.experiment_logs):
            os.mkdir(self.experiment_logs)  # create the experiment log directory

        if not os.path.exists(self.experiment_saved_models):
            os.mkdir(self.experiment_saved_models)  # create the experiment saved models directory

        self.num_epochs = args.num_epochs

#        # Antreas had this but I think it isn't needed if we use a functional loss anyway
#        self.criterion = nn.CrossEntropyLoss().to(self.device)  # send the loss computation to the GPU
        
        if args.continue_from_epoch == -2:
            try:
                self.best_val_model_idx, self.best_val_model_measure, self.state = self.load_model(
                    model_save_dir=self.experiment_saved_models, model_save_name="train_model",
                    model_idx='latest')  # reload existing model from epoch and return best val model index
                # and the best val accuracy of that model
                self.starting_epoch = self.state['current_epoch_idx']
            except:
                print("Model objects cannot be found, initializing a new model and starting from scratch")
                self.starting_epoch = 0
                self.state = dict()

        elif args.continue_from_epoch != -1:  # if continue from epoch is not -1 then
            self.best_val_model_idx, self.best_val_model_measure, self.state = self.load_model(
                model_save_dir=self.experiment_saved_models, model_save_name="train_model",
                model_idx=args.continue_from_epoch)  # reload existing model from epoch and return best val model index
            # and the best val accuracy of that model
            self.starting_epoch = self.state['current_epoch_idx']
        else:
            self.starting_epoch = 0
            self.state = dict()

    def get_num_parameters(self):
        total_num_params = 0
        for param in self.parameters():
            total_num_params += np.prod(param.shape)

        return total_num_params

    def run_train_iter(self, x, y):
        """
        Receives the inputs and targets for the model and runs a training iteration. Returns loss and accuracy metrics.
        :param x: The inputs to the model. A numpy array of shape batch_size, channels, height, width
        :param y: The targets for the model. A numpy array of shape batch_size, num_classes
        :return: the loss and accuracy for this batch
        """
        self.train()  # sets model to training mode (in case batch normalization or other methods have different procedures for training and evaluation)

        out, loss = self.forward_prop_and_loss(x,y)

        # update parameters
        self.optimizer.zero_grad()  # set all weight grads from previous training iters to 0
        loss.backward()  # backpropagate to compute gradients for current iter loss
        self.optimizer.step()  # update network parameters
        
        # return metrics
        if self.task == "classification":
            _, predicted = torch.max(out.data, 1)  # get argmax of predictions
            accuracy = torch.mean(predicted.cpu().eq(y.data).type(torch.float32))  # compute accuracy
            
            normalised_predictions = ((predicted.cpu().type(torch.float32)/255)-0.5)/0.5
            normalised_targets = ((y.type(torch.float32)/255)-0.5)/0.5
            map_mse_range11 = F.mse_loss(normalised_predictions, normalised_targets)# MSE of maximum a posterior predictions, when targets are scaled to range [-1,1] (for consisteny with the regression setting) 
            
            return loss.data.detach().cpu().numpy(), accuracy, map_mse_range11
        
        elif self.task == "regression":
            return loss.data.detach().cpu().numpy()

    def run_evaluation_iter(self, x, y):
        """
        Receives the inputs and targets for the model and runs an evaluation iterations. Returns loss and accuracy metrics.
        :param x: The inputs to the model. A numpy array of shape batch_size, channels, height, width
        :param y: The targets for the model. A numpy array of shape batch_size, num_classes
        :return: the loss and accuracy for this batch
        """
        self.eval()  # sets the system to validation mode

        out, loss = self.forward_prop_and_loss(x,y)

        # return metrics        
        if self.task == "classification":
            _, predicted = torch.max(out.data, 1)  # get argmax of predictions
            accuracy = torch.mean(predicted.cpu().eq(y.data).type(torch.float32))  # compute accuracy
            
            normalised_predictions = ((predicted.cpu().type(torch.float32)/255)-0.5)/0.5
            normalised_targets = ((y.type(torch.float32)/255)-0.5)/0.5
            map_mse_range11 = F.mse_loss(normalised_predictions, normalised_targets)# MSE of maximum a posterior predictions, when targets are scaled to range [-1,1] (for consisteny with the regression setting) 
            
            return loss.data.detach().cpu().numpy(), accuracy, map_mse_range11
        
        elif self.task == "regression":
            return loss.data.detach().cpu().numpy()

    
    def forward_prop_and_loss(self, x, y):
        # reshape inputs and targets
# =============================================================================
#         if self.task == "classification":
# # =============================================================================
# #             if len(y.shape) > 1:
# #                 y = np.argmax(y, axis=1)  # convert one hot encoded labels to single integer labels
# #             if type(x) is np.ndarray:
# #                 x, y = torch.Tensor(x).float().to(device=self.device), torch.Tensor(y).long().to(
# #                 device=self.device)  # convert data to pytorch tensors and send to the computation device
# # =============================================================================
#         elif self.task == "regression":
#             if type(x) is np.ndarray:
#                 x, y = torch.Tensor(x).float().to(device=self.device), torch.Tensor(y).float().to(
#                 device=self.device)  # convert data to pytorch tensors and send to the computation device
# =============================================================================

        x = x.to(self.device)
        y = y.to(self.device)
        out = self.model.forward(x)  # forward the data in the model

        if self.task == "classification":
            if self.loss == "cross_entropy":
                loss = F.cross_entropy(out, y)  # compute loss
        elif self.task == "regression":
            if self.loss == "L2":
                loss = F.mse_loss(out, y)
        
        
        return out, loss



    def save_model(self, model_save_dir, model_save_name, model_idx, state):
        """
        Save the network parameter state and current best val epoch idx and best val accuracy.
        :param model_save_name: Name to use to save model without the epoch index
        :param model_idx: The index to save the model with.
        :param best_validation_model_idx: The index of the best validation model to be stored for future use.
        :param best_validation_model_accuracy: The best validation accuracy to be stored for use at test time.
        :param model_save_dir: The directory to store the state at.
        :param state: The dictionary containing the system state.

        """
        state['network'] = self.state_dict()  # save network parameter and other variables.
        torch.save(state, f=os.path.join(model_save_dir, "{}_{}".format(model_save_name, str(
            model_idx))))  # save state at prespecified filepath

    def load_model(self, model_save_dir, model_save_name, model_idx):
        """
        Load the network parameter state OF THE LATEST EPOCH and the best val model idx and best val accuracy to be compared with the future val accuracies, in order to choose the best val model
        :param model_save_dir: The directory to store the state at.
        :param model_save_name: Name to use to save model without the epoch index
        :param model_idx: The index to save the model with.
        :return: best val idx and best val model accuracy, also it loads the network state into the system state without returning it
        """
        state = torch.load(f=os.path.join(model_save_dir, "{}_{}".format(model_save_name, str(model_idx))))
        self.load_state_dict(state_dict=state['network'])
        return state['best_val_model_idx'], state['best_val_model_measure'], state

    def run_experiment(self):
        """
        Runs experiment train and evaluation iterations, saving the model and best val model and val model accuracy after each epoch
        :return: The summary current_epoch_losses from starting epoch to total_epochs.
        """
        # initialize a dict to keep the per-epoch metrics
        if self.task == "classification":
            total_losses = {"train_accuracy": [], "train_loss": [], "train_map_mse_range11": [],
                            "val_accuracy": [], "val_loss": [], "val_map_mse_range11": [],
                            "curr_epoch": []}
        elif self.task == "regression":
            total_losses = {"train_loss": [],
                        "val_loss": [], "curr_epoch": []}
            
        for i, epoch_idx in enumerate(range(self.starting_epoch, self.num_epochs)):
            epoch_start_time = time.time()
            
            if self.task == "classification":
                current_epoch_losses = {"train_accuracy": [], "train_loss": [], "train_map_mse_range11": [],
                                        "val_accuracy": [], "val_loss": [], "val_map_mse_range11": []}
            elif self.task == "regression":
                current_epoch_losses = {"train_loss": [], "val_loss": []}

            ### training set
            with tqdm.tqdm(total=len(self.train_data)) as pbar_train:  # create a progress bar for training
                for idx, (x, y) in enumerate(self.train_data):  # get data batches
                    if self.task == "classification":
                        loss, accuracy, map_mse_range11 = self.run_train_iter(x=x, y=y)  # take a training iter step
                        self.update_current_epoch_stats(current_epoch_losses, current_dataset="train", loss=loss,accuracy=accuracy, map_mse_range11=map_mse_range11)
                        pbar_train.update(1)
                        pbar_train.set_description("loss: {:.4f}, accuracy: {:.4f}".format(loss, accuracy))
                    elif self.task == "regression":
                        loss = self.run_train_iter(x=x, y=y)  # take a training iter step
                        self.update_current_epoch_stats(current_epoch_losses, current_dataset="train", loss=loss)
                        pbar_train.update(1)
                        pbar_train.set_description("loss: {:.4f}".format(loss))
            
            ### validation set
            with tqdm.tqdm(total=len(self.val_data)) as pbar_val:  # create a progress bar for validation
                for x, y in self.val_data:  # get data batches
                    if self.task == "classification":
                        loss, accuracy, map_mse_range11 = self.run_evaluation_iter(x=x, y=y)  # run a validation iter
                        self.update_current_epoch_stats(current_epoch_losses, current_dataset="val", loss=loss,accuracy=accuracy, map_mse_range11=map_mse_range11)
                        pbar_val.update(1)  # add 1 step to the progress bar
                        pbar_train.set_description("loss: {:.4f}, accuracy: {:.4f}".format(loss, accuracy))
                    elif self.task == "regression":
                        loss = self.run_evaluation_iter(x=x, y=y)  # run a validation iter
                        self.update_current_epoch_stats(current_epoch_losses, current_dataset="val", loss=loss)
                        pbar_val.update(1)  # add 1 step to the progress bar
                        pbar_val.set_description("loss: {:.4f}".format(loss))
            
            self.update_best_epoch_measure(current_epoch_losses, epoch_idx)
            
            
            for key, value in current_epoch_losses.items():
                total_losses[key].append(np.mean(
                    value))  # get mean of all metrics of current epoch metrics dict, to get them ready for storage and output on the terminal.

            total_losses['curr_epoch'].append(epoch_idx)
            save_statistics(experiment_log_dir=self.experiment_logs, filename='summary.csv',
                            stats_dict=total_losses, current_epoch=i,
                            continue_from_mode=True if (self.starting_epoch != 0 or i > 0) else False) # save statistics to stats file.

            # load_statistics(experiment_log_dir=self.experiment_logs, filename='summary.csv') # How to load a csv file if you need to

            out_string = "_".join(
                ["{}_{:.4f}".format(key, np.mean(value)) for key, value in current_epoch_losses.items()])
            # create a string to use to report our epoch metrics
            epoch_elapsed_time = time.time() - epoch_start_time  # calculate time taken for epoch
            epoch_elapsed_time = "{:.4f}".format(epoch_elapsed_time)
            print("Epoch {}:".format(epoch_idx), out_string, "epoch time", epoch_elapsed_time, "seconds")
            self.state['current_epoch_idx'] = epoch_idx
            self.state['best_val_model_measure'] = self.best_val_model_measure
            self.state['best_val_model_idx'] = self.best_val_model_idx
            self.save_model(model_save_dir=self.experiment_saved_models,
                            # save model and best val idx and best val accuracy, using the model dir, model name and model idx
                            model_save_name="train_model", model_idx=epoch_idx, state=self.state)
            self.save_model(model_save_dir=self.experiment_saved_models,
                            # save model and best val idx and best val accuracy, using the model dir, model name and model idx
                            model_save_name="train_model", model_idx='latest', state=self.state)
        ### test set
        print("Generating test set evaluation metrics")
        self.load_model(model_save_dir=self.experiment_saved_models, model_idx=self.best_val_model_idx,
                        # load best validation model
                        model_save_name="train_model")
        
        if self.task == "classification":
            current_epoch_losses = {"test_accuracy": [], "test_loss": [], "test_map_mse_range11": []}  # initialize a statistics dict
        elif self.task == "regression":
            current_epoch_losses = {"test_loss": []}  # initialize a statistics dict


        with tqdm.tqdm(total=len(self.test_data)) as pbar_test:  # ini a progress bar
            for x, y in self.test_data:  # sample batch
                if self.task == "classification":
                    loss, accuracy, map_mse_range11 = self.run_evaluation_iter(x=x, y=y)  # run a validation iter
                    self.update_current_epoch_stats(current_epoch_losses, current_dataset="test", loss=loss,accuracy=accuracy, map_mse_range11=map_mse_range11)
                    pbar_test.update(1)  # add 1 step to the progress bar
                    pbar_test.set_description("loss: {:.4f}, accuracy: {:.4f}".format(loss, accuracy))
                elif self.task == "regression":
                    loss = self.run_evaluation_iter(x=x, y=y)  # run a validation iter
                    self.update_current_epoch_stats(current_epoch_losses, current_dataset="test", loss=loss)
                    pbar_test.update(1)  # add 1 step to the progress bar
                    pbar_test.set_description("loss: {:.4f}".format(loss))


        test_losses = {key: [np.mean(value)] for key, value in
                       current_epoch_losses.items()}  # save test set metrics in dict format
        save_statistics(experiment_log_dir=self.experiment_logs, filename='test_summary.csv',
                        # save test set metrics on disk in .csv format
                        stats_dict=test_losses, current_epoch=0, continue_from_mode=False)
        
        # rename best validation model file for easy access
        path_best = os.path.join(self.experiment_saved_models, "{}_{}".format("train_model", str(self.best_val_model_idx)))
        os.replace(path_best, path_best + "_best")
        
        return total_losses, test_losses
    
    
    def update_current_epoch_stats(self, current_epoch_losses, current_dataset, loss=[], accuracy = [], map_mse_range11 = []):
        if self.task == "classification":
            current_epoch_losses["{}_loss".format(current_dataset)].append(loss)  # add current iter loss to the train loss list
            current_epoch_losses["{}_accuracy".format(current_dataset)].append(accuracy)  # add current iter accuracy to the train accuracy list
            current_epoch_losses["{}_map_mse_range11".format(current_dataset)].append(map_mse_range11)  # add current iter accuracy to the train accuracy list
        elif self.task == "regression":
            current_epoch_losses["{}_loss".format(current_dataset)].append(loss)  # add current iter loss to the train loss list


    def update_best_epoch_measure(self, current_epoch_losses, epoch_idx):    
        """
        Updates statistics for best epoch, if the current epoch is the best epoch so far
        """
        if self.task == "classification":
            val_mean_performance_measure = np.mean(current_epoch_losses['val_loss']) # measure that determines which is the best epoch. For classification: accuracy                    
            if val_mean_performance_measure < self.best_val_model_measure:  # if current epoch's mean performance measure is better than the saved best one then
                self.best_val_model_measure = val_mean_performance_measure  # set the best val model accuracy to be current epoch's val accuracy
                self.best_val_model_idx = epoch_idx  # set the experiment-wise best val idx to be the current epoch's idx
                

        elif self.task == "regression":
            val_mean_performance_measure = np.mean(current_epoch_losses['val_loss']) # measure that determines which is the best epoch. For regression: loss                    
            if val_mean_performance_measure < self.best_val_model_measure:  # if current epoch's mean performance measure is better than the saved best one then
                self.best_val_model_measure = val_mean_performance_measure  # set the best val model accuracy to be current epoch's val accuracy
                self.best_val_model_idx = epoch_idx  # set the experiment-wise best val idx to be the current epoch's idx
        
        
        
class AnomalyDetectionExperiment(nn.Module):
    
    def __init__(self, experiment_name, anomaly_detection_experiment_name,
                 model, device,
                 val_dataset, val_data_loader,
                 test_dataset, test_data_loader,
                 args):
        
        super(AnomalyDetectionExperiment, self).__init__()          
        
        self.measure_of_anomaly=args.measure_of_anomaly
        self.window_aggregation_method=args.window_aggregation_method
        self.save_anomaly_maps=args.save_anomaly_maps
        use_gpu = args.use_gpu
        self.resize_anomaly_maps = True if args.scale_image is not None else False # if images during training were scaled (mostly to be smaller), then anomaly detection automatically happens on the appropriately scaled images. However, the anomaly maps need to be reshaped to the size of the label images before calculating agreement between anomaly maps and ground truth segmentation
        try:
            self.AD_margins = args.AD_margins # Tupel of image margins in image dimensions 1 and 2 that should not be considered for calculating agreement between anomaly map and label image
        except: # None by default
            self.AD_margins = None
        
        self.val_data_loader = val_data_loader
        self.val_dataset = val_dataset # This is needed to get the full size ground truth images
        self.val_image_list = val_dataset.image_list 
        self.val_image_sizes = val_dataset.image_sizes
        
        self.test_data_loader=test_data_loader
        self.test_dataset = test_dataset # This is needed to get the full size ground truth images
        self.test_image_list = test_dataset.image_list 
        self.test_image_sizes = test_dataset.image_sizes
        
        self.model = model
        self.device = device
        
        if torch.cuda.device_count() > 1:
            self.model.to(self.device)
            self.model = nn.DataParallel(module=self.model)
        else:
            self.model.to(self.device)  # sends the model from the cpu to the gpu
        
        # Load state dict from  best epoch of that experiment
        model_dir = os.path.abspath(os.path.join("results", experiment_name, "saved_models"))
        trained_as_parallel_AD_single_process = True if torch.cuda.device_count() < 1 else False
        state_dict = load_best_model_state_dict(model_dir=model_dir, use_gpu=use_gpu, saved_as_parallel_load_as_single_process=trained_as_parallel_AD_single_process)
        self.load_state_dict(state_dict=state_dict["network"]) # Note: You need to load the state dict for the whole AnomalyDetection object, not just the model, since that is the format the state dict was saved in
        
        self.anomaly_map_dir_val = os.path.abspath(os.path.join("results", "anomaly_detection", experiment_name + "___" + anomaly_detection_experiment_name, "anomaly_maps", "val"))
        if not os.path.exists(self.anomaly_map_dir_val):
            os.makedirs(self.anomaly_map_dir_val)
        
        self.anomaly_map_dir_test = os.path.abspath(os.path.join("results", "anomaly_detection", experiment_name + "___" + anomaly_detection_experiment_name, "anomaly_maps", "test"))
        if not os.path.exists(self.anomaly_map_dir_test):
            os.makedirs(self.anomaly_map_dir_test)

        self.result_tables_dir = os.path.abspath(os.path.join("results", "anomaly_detection", experiment_name + "___" + anomaly_detection_experiment_name, "tables"))
        if not os.path.exists(self.result_tables_dir):
            os.makedirs(self.result_tables_dir)


    def run_experiment(self):        
        self.model.eval()
       
       
        for which_set in ["val", "test"]:
            num_finished_images = -1 # the data loader works through the test set images in order. num_finished_images is a counter that ticks up everytime one image is finished
            self.stats_dict = {"aucroc":[]} # a dict that keeps the measures of agreement between pixel-wise anomaly score and ground-truth labels, for each image. Current,y AUC is the only measure.
        
            if which_set == "val":
                data_loader = self.val_data_loader
                dataset = self.val_dataset
                anomaly_map_dir = self.anomaly_map_dir_val
                image_list = self.val_image_list
                image_sizes = self.val_image_sizes
            elif which_set == "test":
                data_loader = self.test_data_loader
                dataset = self.test_dataset
                anomaly_map_dir = self.anomaly_map_dir_test
                image_list = self.test_image_list
                image_sizes = self.test_image_sizes
            if len(image_list) == 0:
                print("All images already have corresponding anomaly maps. No new anomaly maps created.")
                return

            with tqdm.tqdm(total=len(image_list)) as pbar:
                for inputs, targets, image_idxs, slices in data_loader:
                    # calculate "partial" anomaly maps for all patches (not full images!) in the current batch
                    anomaly_maps_current_batch = self.calculate_anomaly_maps(inputs, targets) 
              
                    # the following for-loop deals with translating the pixelwise anomaly for one sliding window position (and thus a score relative to an image patch) into an anomaly score for the full image
                    for batch_idx in range(len(image_idxs)): # for each image in the batch. "in range(batch_size)" leads to error, because the last batch is smaller that the batch size
                        # Get the index of the full image that the current patch was taken from. The index is relative to image_list and image_sizes
                        current_image_idx = int(image_idxs[batch_idx].detach().numpy())
                        assert num_finished_images <= current_image_idx, "This assertion fails if the dataloader does not strucutre the batches so that the order of images/patches WITHIN the batch does still correspond to image_list" # Basically, I am sure that __getitem__() gets items in the right order, but I am unsure if the order gets imxed up within the minibatch by the DataLoader. Probably best to leave that assertion in, since this will throw a bug if the behaviour of DataLoader is changed in future PyTorch versions.
                
                        
                        if current_image_idx > num_finished_images: # Upon starting the with the first patch, or whenever we have moved on to the next image
                            num_finished_images += 1
                            if num_finished_images > 0: # Whenever we have moved to the next image, calculate agreement between our anomaly score and the ground truth segmentation. (Obviously don't do this when we are jstus tarting with the first patch)
                                if self.window_aggregation_method == "mean": # how we normalise the anomaly_map might depend on the window aggregation method
                                    anomaly_map = self.normalise_anomaly_map(anomaly_map,normalisation_map)
                                
                                # load ground truth segmentation label image
                                label_image = dataset.get_label_image(current_image_idx-1)
                                
                                # if scale_image was used during training, resize anomaly map to original image scale
                                if self.resize_anomaly_maps:
                                    anomaly_map = nn.functional.interpolate(anomaly_map.unsqueeze(0), size=(label_image.shape[1], label_image.shape[2])) # introduce batch_size dimension (as required by interpolate) and then scale tensor
                                    anomaly_map = anomaly_map.squeeze(0) # remove batch-size dimension again, to shape C x H x W
                                
                                if self.save_anomaly_maps: # save anomaly map, in same dimensions as original image
                                    torch.save(anomaly_map, os.path.join(anomaly_map_dir, image_list[current_image_idx -1]))
                                    ## !! Note that this saves the anomaly maps with the same file extension as the original images, even if the format is a torch tensor in reality....
                                
                                # remove margin that should not be considered for calculation of AUC and other scores, if desired
                                if self.AD_margins is not None:
                                    slice_considered_for_AD = np.s_[:,
                                                                    self.AD_margins[0]:anomaly_map.shape[1]-self.AD_margins[0],
                                                                    self.AD_margins[1]:anomaly_map.shape[2]-self.AD_margins[1]]
                                    anomaly_map = anomaly_map[slice_considered_for_AD]
                                    label_image = label_image[slice_considered_for_AD]
                                
                                self.calculate_agreement_between_anomaly_score_and_labels(anomaly_map, label_image)

                                # save stats                
                                save_statistics(experiment_log_dir=self.result_tables_dir, filename=which_set +'_summary.csv',
                                                stats_dict=self.stats_dict, current_epoch=current_image_idx-1, continue_from_mode="if_exists", save_full_dict=False) # save statistics to stats file.
 
                                # update progress bar
                                pbar.update(1)
                                

                            # Upon starting the with the first patch, or whenever we have moved on to the next image, create new anomaly maps and normalisation maps
                            current_image_height = image_sizes[current_image_idx][1]
                            current_image_width = image_sizes[current_image_idx][2]

                            
                            anomaly_map = torch.zeros((1,current_image_height, current_image_width)) # anomaly score heat maps for every image. Initialise as constant zero tensor of the same size as the full image
                            normalisation_map = torch.zeros((1,current_image_height, current_image_width)) # for every image, keep score of how often a given pixel has appeared in a sliding window, for calculation of average scores. Initialise as constant zero tensor of the same size as the full image
                        
                        # Now the part that happens for every image-patch(!): update the relevant part of the current anomaly_score map:
                        
                        # build the slice of the output (when doing inpainting:predictions of mask region; when doing autoencoding: patch reconstruction) wrt the full input image.
                        # Note that this has to be done in such an awkward way because the Pyorch DataLoader doesn't pass slices (or lists of slices, ...), but can deal with dicts
                        current_slice = np.s_[:,
                                              np.s_[slices["1_start"][batch_idx]:slices["1_stop"][batch_idx]],
                                              np.s_[slices["2_start"][batch_idx]:slices["2_stop"][batch_idx]]]
                
                        normalisation_map[current_slice] += 1 # it isn't actually used for all window_aggregation_methods
                        
                        if self.window_aggregation_method == "mean":
                            anomaly_map[current_slice] += anomaly_maps_current_batch[batch_idx,:,:,:]
                        elif self.window_aggregation_method == "min":
                            # here we need to da some acrobatic because we want to minimum anomaly score, but the anomaly maps are initialised with zeros.
                            first_time_pixels = torch.eq(normalisation_map[current_slice], 1).type(torch.float) # pixel that appear in a sliding window mask for the first time
                            min_current_and_aggregated_map = torch.min(anomaly_map[current_slice], anomaly_maps_current_batch[batch_idx,:,:,:]) # pixelwise minimum between anomaly map of the current sample and the respective slice of the aggregated, image-wide anomaly map
                            anomaly_map[current_slice] = first_time_pixels * anomaly_maps_current_batch[batch_idx,:,:,:] + (1-first_time_pixels) * min_current_and_aggregated_map # for the pixels that appear in a sliding window mask for the first time, simply copy over the anomaly map. If the pixel was seen before, take the minimum between the previous and the current anomaly score.
                        elif self.window_aggregation_method == "max":
                            anomaly_map[current_slice] = torch.max(anomaly_map[current_slice], anomaly_maps_current_batch[batch_idx,:,:,:])
                           
                    

                
                
                ### ---------------------------------------
                ### also calculate results and save anomaly map for the last image
                if self.window_aggregation_method == "mean": # how we normalise the anomaly_map might depend on the window aggregation method
                    anomaly_map = self.normalise_anomaly_map(anomaly_map,normalisation_map)
                                
                # load ground truth segmentation label image
                label_image = dataset.get_label_image(current_image_idx)
                                
                # if scale_image was used during training, resize anomaly map to original image scale
                if self.resize_anomaly_maps:
                    anomaly_map = nn.functional.interpolate(anomaly_map.unsqueeze(0), size=(label_image.shape[1], label_image.shape[2])) # introduce batch_size dimension (as required by interpolate) and then scale tensor
                    anomaly_map = anomaly_map.squeeze(0) # remove batch-size dimension again, to shape C x H x W
                
                if self.save_anomaly_maps: # save anomaly map, in same dimensions as original image
                    torch.save(anomaly_map, os.path.join(anomaly_map_dir, image_list[current_image_idx]))
                
                # remove margin that should not be considered for calculation of AUC and other scores, if desired
                if self.AD_margins is not None:
                    slice_considered_for_AD = np.s_[:,
                                                    self.AD_margins[0]:anomaly_map.shape[1]-self.AD_margins[0],
                                                    self.AD_margins[1]:anomaly_map.shape[2]-self.AD_margins[1]]
                    anomaly_map = anomaly_map[slice_considered_for_AD]
                    label_image = label_image[slice_considered_for_AD]
                
                self.calculate_agreement_between_anomaly_score_and_labels(anomaly_map, label_image)
                # update progress bar
                pbar.update(1)
                
                # save stats                
                save_statistics(experiment_log_dir=self.result_tables_dir, filename=which_set +'_summary.csv',
                                stats_dict=self.stats_dict, current_epoch=current_image_idx, continue_from_mode=True, save_full_dict=False) # save statistics to stats file.
    
                
                ### ---------------------------------------
                
                
                # print mean results:
                print("{} set results:".format(which_set))
                for key, list_of_values in self.stats_dict.items():
                    list_of_non_nan_values = [x for x in list_of_values if not np.isnan(x)]
                    mean_value = sum(list_of_non_nan_values)/len(list_of_non_nan_values)
                    print("Mean ", key, ": ", "{:.4f}".format(mean_value)) 
        
    def normalise_anomaly_map(self, anomaly_map, normalisation_map):
        # normalise anomaly score maps
        normalisation_map[normalisation_map == 0] = 1 # change zeros in the normalisation factor to 1
        anomaly_map = anomaly_map / normalisation_map
        return anomaly_map


    def calculate_agreement_between_anomaly_score_and_labels(self, anomaly_map, label_image):
        ### calculate measures of agreement 
        # AUC: currently the only measure of agreement
        if self.measure_of_anomaly == "absolute distance" or self.measure_of_anomaly == "likelihood": #then all anomly scores will be in [0,infinity], and higher scores will mean more anomaly, so no further preprocessing is needed to calculate AUC:
            aucroc = get_aucroc(label_image, anomaly_map)            
        
        self.stats_dict["aucroc"].append(aucroc)

          
    def calculate_anomaly_maps(self, inputs, targets):
        # calculate the anomaly map (pixel-wise anomaly score) for the patches in the current batch
        inputs = inputs.to(self.device)
        targets = targets.to(self.device)
        outputs = self.model.forward(inputs)
        
        if self.measure_of_anomaly == "absolute distance":
            anomaly_maps = torch.abs(outputs - targets)
            anomaly_maps = torch.mean(anomaly_maps, dim=1, keepdim=True) # take the mean over the channels
            anomaly_maps = anomaly_maps.cpu().detach()
        elif self.measure_of_anomaly == "likelihood":
#            outputs = F.log_softmax(outputs, dim=1) # outputs have shape batch_size x classes x channels x mask_height x mask_width
            anomaly_maps = F.cross_entropy(outputs, targets, reduction="none")  # pixelwise NLL, dimensions batch_size x channels x mask_height x mask_width
            anomaly_maps = torch.sum(anomaly_maps, dim=1, keepdim=True) # Sum the NLL over the channels
            anomaly_maps = anomaly_maps.cpu().detach()
        return anomaly_maps


# =============================================================================
# 
# ### normalise anomaly score  maps
# for image_idx in range(len(image_list)):
#     normalisation_maps[image_idx][normalisation_maps[image_idx] == 0] = 1 # change zeros in the normalisation factor to 1
#     anomaly_maps[image_idx] = anomaly_maps[image_idx] / normalisation_maps[image_idx]
#     
# 
# 
# # =============================================================================
# # 
# # ### combine anomaly scores - REPLACED BECAUSE OF MEMORY ISSUES
# # all_anomaly_scores = {}
# # for image_name, image_size in zip(image_list, image_sizes):
# #     if window_aggregation_method == "mean":
# #         combined_score_tensor = torch.zeros(image_size)
# #         windows_per_pixel = torch.zeros(image_size) # counts how many times a pixel appears in a window, for averaging
# #         for window_info in all_windows[image_name]:
# #             score_tensor = torch.zeros(image_size)
# #             score_tensor[window_info["slice relative to full image"]] = window_info["anomaly score"]
# #             combined_score_tensor = combined_score_tensor  + score_tensor
# #             windows_per_pixel[window_info["slice relative to full image"]] += 1
# #         windows_per_pixel[windows_per_pixel == 0] = 1
# #         combined_score_tensor = combined_score_tensor / windows_per_pixel
# #         all_anomaly_scores[image_name] = combined_score_tensor
# #         
# # =============================================================================
# ### testing
# show_idx = 1
# anomaly_score = anomaly_maps[show_idx].detach().numpy()
# anomaly_score = np.squeeze(anomaly_score)
# normalisation_map = normalisation_maps[show_idx].detach().numpy()
# normalisation_map = np.squeeze(normalisation_map)
# 
# plt.figure()
# plt.imshow(anomaly_score)
# plt.figure()
# plt.imshow(normalisation_map)
# 
# #%%
# ### Compare anomaly heat map with ground-truth labels:
# from sklearn.metrics import roc_auc_score
# def get_aucroc(y_true, output):
#     if torch.min(y_true.data) == 1 or torch.max(y_true.data) == 0:
#         aucroc = np.nan # return nan if there are only examples of one type in the batch, because AUCROC is not defined then. 
#     else:
#         y_true = y_true.cpu().detach().numpy().flatten()
#         output = output.cpu().detach().numpy().flatten()
#         aucroc = roc_auc_score(y_true,output)
#     return aucroc
# 
# 
# aucroc_per_image = np.empty(len(image_list))
# for idx, anomaly_map in enumerate(anomaly_maps):
#     label_image = data.get_label_image(idx)
#     
#     if measure_of_anomaly == "absolute distance": #then all anoamly scores will be in [0,1]:
#         aucroc_per_image[idx] = get_aucroc(label_image, anomaly_map)
#         
# aucroc_mn = np.mean(aucroc_per_image)
#         
#     
# =============================================================================
