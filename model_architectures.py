import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


class FCCNetwork(nn.Module):
    def __init__(self, input_shape, num_output_classes, num_filters, num_layers, use_bias=False):
        """
        Initializes a fully connected network similar to the ones implemented previously in the MLP package.
        :param input_shape: The shape of the inputs going in to the network.
        :param num_output_classes: The number of outputs the network should have (for classification those would be the number of classes)
        :param num_filters: Number of filters used in every fcc layer.
        :param num_layers: Number of fcc layers (excluding dim reduction stages)
        :param use_bias: Whether our fcc layers will use a bias.
        """
        super(FCCNetwork, self).__init__()
        # set up class attributes useful in building the network and inference
        self.input_shape = input_shape
        self.num_filters = num_filters
        self.num_output_classes = num_output_classes
        self.use_bias = use_bias
        self.num_layers = num_layers
        # initialize a module dict, which is effectively a dictionary that can collect layers and integrate them into pytorch
        self.layer_dict = nn.ModuleDict()
        # build the network
        self.build_module()

    def build_module(self):
        print("Building basic block of FCCNetwork using input shape", self.input_shape)
        x = torch.zeros((self.input_shape))

        out = x
        out = out.view(out.shape[0], -1)
        # flatten inputs to shape (b, -1) where -1 is the dim resulting from multiplying the
        # shapes of all dimensions after the 0th dim

        for i in range(self.num_layers):
            self.layer_dict['fcc_{}'.format(i)] = nn.Linear(in_features=out.shape[1],  # initialize a fcc layer
                                                            out_features=self.num_filters,
                                                            bias=self.use_bias)

            out = self.layer_dict['fcc_{}'.format(i)](out)  # apply ith fcc layer to the previous layers outputs
            out = F.relu(out)  # apply a ReLU on the outputs

        self.logits_linear_layer = nn.Linear(in_features=out.shape[1],  # initialize the prediction output linear layer
                                             out_features=self.num_output_classes,
                                             bias=self.use_bias)
        out = self.logits_linear_layer(out)  # apply the layer to the previous layer's outputs
        print("Block is built, output volume is", out.shape)
        return out

    def forward(self, x):
        """
        Forward prop data through the network and return the preds
        :param x: Input batch x a batch of shape batch number of samples, each of any dimensionality.
        :return: preds of shape (b, num_classes)
        """
        out = x
        out = out.view(out.shape[0], -1)
        # flatten inputs to shape (b, -1) where -1 is the dim resulting from multiplying the
        # shapes of all dimensions after the 0th dim

        for i in range(self.num_layers):
            out = self.layer_dict['fcc_{}'.format(i)](out)  # apply ith fcc layer to the previous layers outputs
            out = F.relu(out)  # apply a ReLU on the outputs

        out = self.logits_linear_layer(out)  # apply the layer to the previous layer's outputs
        return out

    def reset_parameters(self):
        """
        Re-initializes the networks parameters
        """
        for item in self.layer_dict.children():
            item.reset_parameters()

        self.logits_linear_layer.reset_parameters()

class ConvolutionalNetwork(nn.Module):
    def __init__(self, input_shape, dim_reduction_type, num_output_classes, num_filters, num_layers, use_bias=False):
        """
        Initializes a convolutional network module object.
        :param input_shape: The shape of the inputs going in to the network.
        :param dim_reduction_type: The type of dimensionality reduction to apply after each convolutional stage, should be one of ['max_pooling', 'avg_pooling', 'strided_convolution', 'dilated_convolution']
        :param num_output_classes: The number of outputs the network should have (for classification those would be the number of classes)
        :param num_filters: Number of filters used in every conv layer, except dim reduction stages, where those are automatically infered.
        :param num_layers: Number of conv layers (excluding dim reduction stages)
        :param use_bias: Whether our convolutions will use a bias.
        """
        super(ConvolutionalNetwork, self).__init__()
        # set up class attributes useful in building the network and inference
        self.input_shape = input_shape
        self.num_filters = num_filters
        self.num_output_classes = num_output_classes
        self.use_bias = use_bias
        self.num_layers = num_layers
        self.dim_reduction_type = dim_reduction_type
        # initialize a module dict, which is effectively a dictionary that can collect layers and integrate them into pytorch
        self.layer_dict = nn.ModuleDict()
        # build the network
        self.build_module()

    def build_module(self):
        """
        Builds network whilst automatically inferring shapes of layers.
        """
        print("Building basic block of ConvolutionalNetwork using input shape", self.input_shape)
        x = torch.zeros((self.input_shape))  # create dummy inputs to be used to infer shapes of layers

        out = x
        # torch.nn.Conv2d(in_channels, out_channels, kernel_size, stride=1, padding=0, dilation=1, groups=1, bias=True)
        for i in range(self.num_layers):  # for number of layers times
            self.layer_dict['conv_{}'.format(i)] = nn.Conv2d(in_channels=out.shape[1],
                                                             # add a conv layer in the module dict
                                                             kernel_size=3,
                                                             out_channels=self.num_filters, padding=1,
                                                             bias=self.use_bias)

            out = self.layer_dict['conv_{}'.format(i)](out)  # use layer on inputs to get an output
            out = F.relu(out)  # apply relu
            print(out.shape)
            if self.dim_reduction_type == 'strided_convolution':  # if dim reduction is strided conv, then add a strided conv
                self.layer_dict['dim_reduction_strided_conv_{}'.format(i)] = nn.Conv2d(in_channels=out.shape[1],
                                                                                       kernel_size=3,
                                                                                       out_channels=out.shape[1],
                                                                                       padding=1,
                                                                                       bias=self.use_bias, stride=2,
                                                                                       dilation=1)

                out = self.layer_dict['dim_reduction_strided_conv_{}'.format(i)](
                    out)  # use strided conv to get an output
                out = F.relu(out)  # apply relu to the output
            elif self.dim_reduction_type == 'dilated_convolution':  # if dim reduction is dilated conv, then add a dilated conv, using an arbitrary dilation rate of i + 2 (so it gets smaller as we go, you can choose other dilation rates should you wish to do it.)
                self.layer_dict['dim_reduction_dilated_conv_{}'.format(i)] = nn.Conv2d(in_channels=out.shape[1],
                                                                                       kernel_size=3,
                                                                                       out_channels=out.shape[1],
                                                                                       padding=1,
                                                                                       bias=self.use_bias, stride=1,
                                                                                       dilation=i + 2)
                out = self.layer_dict['dim_reduction_dilated_conv_{}'.format(i)](
                    out)  # run dilated conv on input to get output
                out = F.relu(out)  # apply relu on output

            elif self.dim_reduction_type == 'max_pooling':
                self.layer_dict['dim_reduction_max_pool_{}'.format(i)] = nn.MaxPool2d(2, padding=1)
                out = self.layer_dict['dim_reduction_max_pool_{}'.format(i)](out)

            elif self.dim_reduction_type == 'avg_pooling':
                self.layer_dict['dim_reduction_avg_pool_{}'.format(i)] = nn.AvgPool2d(2, padding=1)
                out = self.layer_dict['dim_reduction_avg_pool_{}'.format(i)](out)

            print(out.shape)
        if out.shape[-1] != 2:
            out = F.adaptive_avg_pool2d(out,
                                        2)  # apply adaptive pooling to make sure output of conv layers is always (2, 2) spacially (helps with comparisons).
        print('shape before final linear layer', out.shape)
        out = out.view(out.shape[0], -1)
        self.logit_linear_layer = nn.Linear(in_features=out.shape[1],  # add a linear layer
                                            out_features=self.num_output_classes,
                                            bias=self.use_bias)
        out = self.logit_linear_layer(out)  # apply linear layer on flattened inputs
        print("Block is built, output volume is", out.shape)
        return out

    def forward(self, x):
        """
        Forward propages the network given an input batch
        :param x: Inputs x (b, c, h, w)
        :return: preds (b, num_classes)
        """
        out = x
        for i in range(self.num_layers):  # for number of layers

            out = self.layer_dict['conv_{}'.format(i)](out)  # pass through conv layer indexed at i
            out = F.relu(out)  # pass conv outputs through ReLU
            if self.dim_reduction_type == 'strided_convolution':  # if strided convolution dim reduction then
                out = self.layer_dict['dim_reduction_strided_conv_{}'.format(i)](
                    out)  # pass previous outputs through a strided convolution indexed i
                out = F.relu(out)  # pass strided conv outputs through ReLU

            elif self.dim_reduction_type == 'dilated_convolution':
                out = self.layer_dict['dim_reduction_dilated_conv_{}'.format(i)](out)
                out = F.relu(out)

            elif self.dim_reduction_type == 'max_pooling':
                out = self.layer_dict['dim_reduction_max_pool_{}'.format(i)](out)

            elif self.dim_reduction_type == 'avg_pooling':
                out = self.layer_dict['dim_reduction_avg_pool_{}'.format(i)](out)

        if out.shape[-1] != 2:
            out = F.adaptive_avg_pool2d(out, 2)
        out = out.view(out.shape[0], -1)  # flatten outputs from (b, c, h, w) to (b, c*h*w)
        out = self.logit_linear_layer(out)  # pass through a linear layer to get logits/preds
        return out

    def reset_parameters(self):
        """
        Re-initialize the network parameters.
        """
        for item in self.layer_dict.children():
            try:
                item.reset_parameters()
            except:
                pass

        self.logit_linear_layer.reset_parameters()

class CE_netG(nn.Module): # generator of a context encoder
    def __init__(self, args):
        """
        Inputs:
            batch_size: int, number of images in batch
            num_image_channels, image_height image_width: int, input image dimensions
            version: str, options are "deterministic" for the standard context encoder 
                and "probabilistic" for the probabilistic context encoder that ends in an 256 class softmax 
            num_channels_bottleneck: int, number of channels and thereby units in the bottleneck layer
            num_layers_decoder: int, number of layers in the decoder. 
                The default of 5 results in an output of 64x64 pixels, can be set to 4 for output of size 32x32
            num_channels_progression_dec: list of int. Determines the number of channels in the 
                decoding layers. Needs to be adjusted if the num_layers_decoder is adjusted. 
                The numbers are multipliers that are applied to the base number 
                of channels (64). For example, the default [8,4,2,1] means that the output from the first 
                deconvolutional layer has 512 channels, the output from the second layer has 256 channels, and so on.
                The dimension of the output of the last layer is determined by num_image_channels.
            
        """
     
        super(CE_netG, self).__init__()

        
        self.input_shape = (args.batch_size, args.num_image_channels, args.image_height, args.image_width)
        
        # hyperparameters for both encoder and decoder
        self.kernel_size = args.kernel_size
        
        # encoder hyperparameters
        self.num_layers_enc = args.num_layers_enc
        self.num_channels_enc = args.num_channels_enc
        self.num_channels_progression_enc = args.num_channels_progression_enc
        self.num_channels_bottleneck = args.num_channels_bottleneck
        
        # decoder hyperparameters
        self.num_layers_dec = args.num_layers_dec
        self.num_channels_dec = args.num_channels_dec
        self.num_channels_progression_dec = args.num_channels_progression_dec
        try: # this is a super clumsy way to set a default value
            self.output_softmax = True if args.task == "classification" else False# to have the output give 255 units for every channel, to go into a softmax
        except:
            self.output_softmax = False
        
        self.layer_dict = nn.ModuleDict() # this dictionary will store the layers
        self.build_module()
        
    def build_module(self):
        """
        Automatically build the model by propagating a dummy input through the layers and infering layer parameters
        """
        
        x = torch.zeros(self.input_shape) # dummy input
        out = x
        
        # encoder
        for i in range(self.num_layers_enc):
            
            # conv layer
            if i < self.num_layers_enc-1: # the non-final layers of the encoder, uses convolution with stride 2 and padding 1, and increase the number of channels according to self.num_channels_progression_enc 
                self.layer_dict["conv_{}".format(i)] = nn.Conv2d(in_channels=out.shape[1],
                                out_channels=self.num_channels_enc*self.num_channels_progression_enc[i],
                                kernel_size=self.kernel_size, stride=2, padding=1, bias=False)
            else: # in final layer, use adaptive kernel size to reduce feature maps to 1x1, and num_bottleneck channels:
                self.layer_dict["conv_{}".format(i)] = nn.Conv2d(in_channels=out.shape[1],
                                out_channels=self.num_channels_bottleneck,
                                kernel_size=out.shape[2], stride=1, padding=0, bias=False)
            out = self.layer_dict["conv_{}".format(i)](out)
            
            # batch norm layer
            if i > 0: # first layer doesn't have batch_norm:
                self.layer_dict["batch_norm_enc{}".format(i)] = nn.BatchNorm2d(out.shape[1])
                out = self.layer_dict["batch_norm_enc{}".format(i)](out)
            
            # leaky ReLU layer
            self.layer_dict["lReLU_{}".format(i)] = nn.LeakyReLU(0.2, inplace=True)
            out = self.layer_dict["lReLU_{}".format(i)](out)
            
        # decoder
        num_layers_total = self.num_layers_enc+self.num_layers_dec
        ind_dec = -1 # index relative to decoder
        for i in range(self.num_layers_enc, num_layers_total):
            ind_dec += 1
            
            # deconvolution layer
            if i == self.num_layers_enc: # first deconv layers has stride 1 and padding 0, since the feature maps of the input to this layer are of dimension 1x1
                self.layer_dict["conv_t_{}".format(i)] = nn.ConvTranspose2d(in_channels=out.shape[1], 
                            out_channels = self.num_channels_dec*self.num_channels_progression_dec[ind_dec],
                            kernel_size=self.kernel_size, stride=1, padding=0, bias=False)
            elif i < num_layers_total - 1:# all deconv layers that aren't the first or the last have stride 2 and padding 1 the double the feature map width and height after every layer
                self.layer_dict["conv_t_{}".format(i)] = nn.ConvTranspose2d(in_channels=out.shape[1], 
                            out_channels = self.num_channels_dec*self.num_channels_progression_dec[ind_dec],
                            kernel_size=self.kernel_size, stride=2, padding=1, bias=False)
            else: # the final deconvolutional layer depends on whether we want a deterministic or a probabilistic context encoder
                if not self.output_softmax:  # deterministic context encoder
                    out_channels = self.input_shape[1] # output pixel values directly, to be used with e.g. MSE
                else: # probabilistic context encoder
                    out_channels = self.input_shape[1]*256 # output units to go into softmax, to be used with likelihood based training
            
                self.layer_dict["conv_t_{}".format(i)] = nn.ConvTranspose2d(in_channels=out.shape[1], 
                            out_channels = out_channels,
                            kernel_size=self.kernel_size, stride=2, padding=1, bias=False)
            
            out = self.layer_dict["conv_t_{}".format(i)](out)
            
            # batch norm layer
            if i < num_layers_total - 1: # last layer doesn't have batch norm:
                self.layer_dict["batch_norm_dec_{}".format(i)] = nn.BatchNorm2d(out.shape[1])
                out =  self.layer_dict["batch_norm_dec_{}".format(i)](out)
            
            # activation layers
            if i < num_layers_total - 1: # non-final layers have ReLU:
                self.layer_dict["ReLU_{}".format(i)] = nn.ReLU(inplace=True)
                out = self.layer_dict["ReLU_{}".format(i)](out)
            else: # the final layer activation  depends on whether we want a deterministic or a probabilistic context encoder
                if not self.output_softmax: # if the model is supposed to output the units for softmax, softmax will be applied later (during loss function)
                    self.layer_dict["tanh_{}".format(i)] = nn.Tanh()
                    out = self.layer_dict["tanh_{}".format(i)](out)
                # probabilistic context encoder: the softmax will be applied later, to make use of PyTorch's cross_entropy loss functions that integrate softmax with NLL loss
                    

    def forward(self, x):
        for layer in self.layer_dict.values():
            x = layer(x)
        if self.output_softmax: # reshape for multidim cross-entropy loss
            new_shape = (x.shape[0], 256, self.input_shape[1], x.shape[2], x.shape[3]) # batch size x classes x channels x output height x output width
            x = x.view(new_shape)
        return x
    
                

# =================Description of layer sizes (in the base version):============================================================
# 
#         self.main = nn.Sequential(
#             # input is (nc) x 128 x 128
#             nn.Conv2d(in_channels=nc,out_channels=nef,kernel_size=4,stride=2,padding=1, bias=False),
#             nn.LeakyReLU(0.2, inplace=True),
#             # state size: (nef) x 64 x 64
#             nn.Conv2d(nef,nef,4,2,1, bias=False),
#             nn.BatchNorm2d(nef),
#             nn.LeakyReLU(0.2, inplace=True),
#             # state size: (nef) x 32 x 32
#             nn.Conv2d(nef,nef*2,4,2,1, bias=False),
#             nn.BatchNorm2d(nef*2),
#             nn.LeakyReLU(0.2, inplace=True),
#             # state size: (nef*2) x 16 x 16
#             nn.Conv2d(nef*2,nef*4,4,2,1, bias=False),
#             nn.BatchNorm2d(nef*4),
#             nn.LeakyReLU(0.2, inplace=True),
#             # state size: (nef*4) x 8 x 8
#             nn.Conv2d(nef*4,nef*8,4,2,1, bias=False),
#             nn.BatchNorm2d(nef*8),
#             nn.LeakyReLU(0.2, inplace=True),
#             # state size: (nef*8) x 4 x 4
#             nn.Conv2d(nef*8,nBottleneck,4, bias=False),
#             # state size: (nBottleneck) x 1 x 1
#             nn.BatchNorm2d(nBottleneck),
#             nn.LeakyReLU(0.2, inplace=True),
        
#             # input is Bottleneck, going into a convolution
#             nn.ConvTranspose2d(nBottleneck, ngf * 8, 4, 1, 0, bias=False),
#             nn.BatchNorm2d(ngf * 8),
#             nn.ReLU(True),
#             # state size. (ngf*8) x 4 x 4
#             nn.ConvTranspose2d(ngf * 8, ngf * 4, 4, 2, 1, bias=False),
#             nn.BatchNorm2d(ngf * 4),
#             nn.ReLU(True),
#             # state size. (ngf*4) x 8 x 8
#             nn.ConvTranspose2d(ngf * 4, ngf * 2, 4, 2, 1, bias=False),
#             nn.BatchNorm2d(ngf * 2),
#             nn.ReLU(True),
#             # state size. (ngf*2) x 16 x 16
#             nn.ConvTranspose2d(ngf * 2, ngf, 4, 2, 1, bias=False),
#             nn.BatchNorm2d(ngf),
#             nn.ReLU(True),
#             # state size. (ngf) x 32 x 32
#             nn.ConvTranspose2d(ngf, nc, 4, 2, 1, bias=False),
#             nn.Tanh()
#             # state size. (nc) x 64 x 64
#         )
# 
# =============================================================================

    
    def reset_parameters(self):
        # custom weights initialization called on netG and netD
        for m in self.layer_dict.children():
            classname = m.__class__.__name__
            if classname.find('Conv') != -1: # if name contains "Conv"
                m.weight.data.normal_(0.0, 0.02)
            elif classname.find('BatchNorm') != -1:
                m.weight.data.normal_(1.0, 0.02)
                m.bias.data.fill_(0)



class CE_netlocalD(nn.Module): # context encoder discriminator network
    def __init__(self, opt):
        super(_netlocalD, self).__init__()
        self.ngpu = opt.ngpu
        self.main = nn.Sequential(
            # input is (nc) x 64 x 64
            nn.Conv2d(opt.nc, opt.ndf, 4, 2, 1, bias=False),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf) x 32 x 32
            nn.Conv2d(opt.ndf, opt.ndf * 2, 4, 2, 1, bias=False),
            nn.BatchNorm2d(opt.ndf * 2),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf*2) x 16 x 16
            nn.Conv2d(opt.ndf * 2, opt.ndf * 4, 4, 2, 1, bias=False),
            nn.BatchNorm2d(opt.ndf * 4),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf*4) x 8 x 8
            nn.Conv2d(opt.ndf * 4, opt.ndf * 8, 4, 2, 1, bias=False),
            nn.BatchNorm2d(opt.ndf * 8),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf*8) x 4 x 4
            nn.Conv2d(opt.ndf * 8, 1, 4, 1, 0, bias=False),
            nn.Sigmoid()
        )

    def forward(self, input):
        output = self.main(input)

        return output.view(-1, 1)
    
    def reset_parameters(self):
        # custom weights initialization called on netG and netD
        for m in self.main:
            classname = m.__class__.__name__
            if classname.find('Conv') != -1: # if name contains "Conv"
                m.weight.data.normal_(0.0, 0.02)
            elif classname.find('BatchNorm') != -1:
                m.weight.data.normal_(1.0, 0.02)
                m.bias.data.fill_(0)
                
                
def create_model(args):
    if args.model_name == "custom_conv_net":
        model = ConvolutionalNetwork(
                    input_shape=(args.batch_size, args.num_image_channels, args.image_height, args.image_width),
                    dim_reduction_type=args.dim_reduction_type, num_filters=args.num_filters, num_layers=args.num_layers, use_bias=False,
                    num_output_classes=args.num_output_classes)
    elif args.model_name == "context_encoder":
        model = CE_netG(args)

    return model


