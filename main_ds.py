import network
import utils
import os
import random
import argparse
import numpy as np

from datasets import VOCSegmentation, Cityscapes, camvids
from utils import ext_transforms as et
from utils import corr_ts as train_et
from metrics import StreamSegMetrics

import torch
import torch.nn as nn

import torch.distributed as dist
import torch.utils.data.distributed as ddist

from torch.utils import data
import torch.nn.functional as F

from PIL import Image
import matplotlib
import matplotlib.pyplot as plt
from datasets.voc import collate_fn2

from metrics.losses import ssp_loss, Mixed_Loss, ssp_loss_inner, PGC_loss




def get_argparser():
    parser = argparse.ArgumentParser()
    # Datset Options
    parser.add_argument("--pgc_mode", type=int, default=0)
    parser.add_argument("--alpha", type=float, default=0.2)
    parser.add_argument("--beta", type=float, default=0.9)
    # parser.add_argument("--data_root", type=str, default='/seu_share/home/wkyang/datasets/CamVid/adapt_data/',
#    parser.add_argument("--data_root", type=str, default=r'D:\datasets\VOC2012\VOCtrainval_11-May-2012',
    parser.add_argument("--data_root", type=str, default='/seu_share/home/wkyang/olddata/wkyang/dataset/VOC/VOC12/',
                        help="path to Dataset")
    parser.add_argument("--dataset", type=str, default='voc',
                        choices=['voc', 'camvids', 'cityscapes'], help='Name of dataset')
    parser.add_argument("--num_classes", type=int, default=None,
                        help="num classes (default: None)")

    # Deeplab Options
    parser.add_argument("--model", type=str, default='deeplabv3plus_resnet101',
                        choices=['deeplabv3_resnet50', 'deeplabv3plus_resnet50',
                                'deeplabv3_resnet101', 'deeplabv3plus_resnet101',
                                'deeplabv3_mobilenet', 'deeplabv3plus_mobilenet'], help='model name')
    parser.add_argument("--separable_conv", action='store_true', default=False,
                        help="apply separable conv to decoder and aspp")
    parser.add_argument("--output_stride", type=int,
                        default=16, choices=[8, 16])

    # Train Options
    parser.add_argument("--test_only", action='store_true', default=False)
    parser.add_argument("--save_val_results", action='store_true', default=True,
                        help="save segmentation results to \"./results\"")
    parser.add_argument("--total_itrs", type=int, default=30e3,
                        help="epoch number (default: 30k)")
    parser.add_argument("--lr", type=float, default=0.025,
                        help="learning rate (default: 0.01)")
    parser.add_argument("--lr_policy", type=str, default='poly', choices=['poly', 'step'],
                        help="learning rate scheduler policy")
    parser.add_argument("--step_size", type=int, default=10000)
    parser.add_argument("--crop_val", action='store_true', default=False,
                        help='crop validation (default: False)')
    parser.add_argument("--batch_size", type=int, default=16,
                        help='batch size (default: 16)')
    parser.add_argument("--val_batch_size", type=int, default=4,
                        help='batch size for validation (default: 4)')
    parser.add_argument("--crop_size", type=int, default=513)

    parser.add_argument("--ckpt", default=None, type=str,
                        help="restore from checkpoint")
    parser.add_argument("--continue_training",
                        action='store_true', default=False)

    parser.add_argument("--loss_type", type=str, default='cross_entropy',
                        choices=['cross_entropy', 'focal_loss'], help="loss type (default: False)")
    parser.add_argument("--gpu_id", type=str, default='0',
                        help="GPU ID")
    parser.add_argument("--weight_decay", type=float, default=1e-4,
                        help='weight decay (default: 1e-4)')
    parser.add_argument("--random_seed", type=int, default=1,
                        help="random seed (default: 1)")
    parser.add_argument("--print_interval", type=int, default=10,
                        help="print interval of loss (default: 10)")
    parser.add_argument("--val_interval", type=int, default=100,
                        help="epoch interval for eval (default: 100)")
    parser.add_argument("--download", action='store_true', default=False,
                        help="download datasets")

    # PASCAL VOC Options
    parser.add_argument("--year", type=str, default='2012_aug',
                        choices=['2012_aug', '2012', '2011', '2009', '2008', '2007'], help='year of VOC')
    parser.add_argument("--num_copys", type=int, default=2)
    # Visdom options
    parser.add_argument("--enable_vis", action='store_true', default=False,
                        help="use visdom for visualization")
    parser.add_argument("--vis_port", type=str, default='13570',
                        help='port for visdom')
    parser.add_argument("--vis_env", type=str, default='main',
                        help='env for visdom')
    parser.add_argument("--vis_num_samples", type=int, default=8,
                        help='number of samples for visualization (default: 8)')
    parser.add_argument("--rank", type=int, default=0, help="rank of current process")
    parser.add_argument("--word_size", default=2, help="word size")
    parser.add_argument("--init_method", default='tcp://127.0.0.0:23456', help="init-method")

    return parser


def get_dataset(opts):
    """ Dataset And Augmentation
    """

    if opts.dataset == 'camvids':
        mean, std = camvids.get_norm()
        train_transform = train_et.ExtCompose([
            # et.ExtResize(size=opts.crop_size),
            train_et.ExtRandomScale((0.5, 2.0)),
            train_et.ExtRandomHorizontalFlip(),
            train_et.New_ExtRandomCrop(
                size=(481, 481), pad_if_needed=True),
            train_et.ExtToTensor(),
            train_et.ExtNormalize(mean=mean, std=std),
        ])
        if opts.crop_val:
            val_transform = et.ExtCompose([
                et.ExtResize(opts.crop_size),
                et.ExtCenterCrop(opts.crop_size),
                et.ExtToTensor(),
                et.ExtNormalize(mean=mean, std=std),
            ])
        else:
            val_transform = et.ExtCompose([
                et.ExtToTensor(),
                et.ExtNormalize(mean=mean, std=std),
            ])
        train_dst = camvids.CamvidSegmentation(opts.data_root, image_set='trainval', transform=train_transform, num_copys=opts.num_copys)
        val_dst = camvids.CamvidSegmentation(opts.data_root, image_set='test', transform=val_transform)

    if opts.dataset == 'voc':
        # train_transform = et.ExtCompose([
        #     #et.ExtResize(size=opts.crop_size),
        #     et.ExtRandomScale((0.5, 2.0)),
        #     et.ExtRandomCrop(size=(opts.crop_size, opts.crop_size), pad_if_needed=True),
        #     et.ExtRandomHorizontalFlip(),
        #     et.ExtToTensor(),
        #     et.ExtNormalize(mean=[0.485, 0.456, 0.406],
        #                     std=[0.229, 0.224, 0.225]),
        # ])
        train_transform = train_et.ExtCompose([
            train_et.ExtRandomScale((0.5, 2.0)),
            train_et.ExtRandomHorizontalFlip(),
            train_et.New_ExtRandomCrop(
                size=(opts.crop_size, opts.crop_size), pad_if_needed=True),
            train_et.ExtToTensor(),
            train_et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                                std=[0.229, 0.224, 0.225]),
        ])
        if opts.crop_val:
            val_transform = et.ExtCompose([
                et.ExtResize(opts.crop_size),
                et.ExtCenterCrop(opts.crop_size),
                et.ExtToTensor(),
                et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                                std=[0.229, 0.224, 0.225]),
            ])
        else:
            val_transform = et.ExtCompose([
                et.ExtToTensor(),
                et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                                std=[0.229, 0.224, 0.225]),
            ])
        train_dst = VOCSegmentation(root=opts.data_root, year=opts.year,
                                    image_set='train', download=opts.download, transform=train_transform, num_copys=opts.num_copys)
        val_dst = VOCSegmentation(root=opts.data_root, year=opts.year,
                                image_set='val', download=False, transform=val_transform)

    if opts.dataset == 'cityscapes':
        train_transform = et.ExtCompose([
            # et.ExtResize( 512 ),
            et.ExtRandomCrop(size=(opts.crop_size, opts.crop_size)),
            et.ExtColorJitter(brightness=0.5, contrast=0.5, saturation=0.5),
            et.ExtRandomHorizontalFlip(),
            et.ExtToTensor(),
            et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                            std=[0.229, 0.224, 0.225]),
        ])

        val_transform = et.ExtCompose([
            # et.ExtResize( 512 ),
            et.ExtToTensor(),
            et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                            std=[0.229, 0.224, 0.225]),
        ])

        train_dst = Cityscapes(root=opts.data_root, split='train', transform=train_transform, num_copys=opts.num_copys)
        print("------------------------now copy: {:}----------------------------------".format(opts.num_copys))
        val_dst = Cityscapes(root=opts.data_root, split='val', transform=val_transform)
    return train_dst, val_dst


def validate(opts, model, loader, device, metrics, ret_samples_ids=None):
    """Do validation and return specified samples"""
    metrics.reset()
    ret_samples = []
    if opts.save_val_results:
        if not os.path.exists('results'):
            os.mkdir('results')
        denorm = utils.Denormalize(mean=[0.485, 0.456, 0.406],
                                std=[0.229, 0.224, 0.225])
        img_id = 0

    with torch.no_grad():
        for i, (images, labels) in (enumerate(loader)):

            images = images.to(device, dtype=torch.float32)
            labels = labels.to(device, dtype=torch.long)

            outputs = model(images)
            preds = outputs[-1].detach().max(dim=1)[1].cpu().numpy()
            targets = labels.cpu().numpy()

            metrics.update(targets, preds)
            if ret_samples_ids is not None and i in ret_samples_ids:  # get vis samples
                ret_samples.append(
                    (images[0].detach().cpu().numpy(), targets[0], preds[0]))

        # if opts.save_val_results:
        #     for i in range(len(images)):
        #         image = images[i].detach().cpu().numpy()
        #         target = targets[i]
        #         pred = preds[i]

        #         image = (denorm(image) * 255).transpose(1, 2, 0).astype(np.uint8)
        #         target = loader.dataset.decode_target(target).astype(np.uint8)
        #         pred = loader.dataset.decode_target(pred).astype(np.uint8)

        #         Image.fromarray(image).save('results/%d_image.png' % img_id)
        #         Image.fromarray(target).save('results/%d_target.png' % img_id)
        #         Image.fromarray(pred).save('results/%d_pred.png' % img_id)

        #         fig = plt.figure()
        #         plt.imshow(image)
        #         plt.axis('off')
        #         plt.imshow(pred, alpha=0.7)
        #         ax = plt.gca()
        #         ax.xaxis.set_major_locator(matplotlib.ticker.NullLocator())
        #         ax.yaxis.set_major_locator(matplotlib.ticker.NullLocator())
        #         plt.savefig('results/%d_overlay.png' % img_id, bbox_inches='tight', pad_inches=0)
        #         plt.close()
        #         img_id += 1

        score = metrics.get_results()
    return score, ret_samples


def main():
    opts = get_argparser().parse_args()

    dist.init_process_group(backend='nccl', init_method=opts.init_method, rank=opts.rank, world_size=opts.world_size)

    if opts.dataset.lower() == 'voc':
        opts.num_classes = 21
    elif opts.dataset.lower() == 'cityscapes':
        opts.num_classes = 19
    elif opts.dataset.lower() == 'camvids':
        opts.num_classes = 12

    # Setup visualization

    #os.environ['CUDA_VISIBLE_DEVICES'] = opts.gpu_id
    Device = os.environ['CUDA_VISIBLE_DEVICES'].split(',')
    Device = [int(x) for x in Device]

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print("Device: %s" % device)

    # Setup random seed
    torch.manual_seed(opts.random_seed)
    np.random.seed(opts.random_seed)
    random.seed(opts.random_seed)

    # Setup dataloader
    if opts.dataset == 'voc' and not opts.crop_val:
        opts.val_batch_size = 1

    for name, value in opts._get_kwargs():
        print('{:16} : {:}'.format(name, value))

    train_dst, val_dst = get_dataset(opts)

    sampler = ddist.DistributedSampler(train_dst, shuffle=True)
    val_sampler = ddist.DistributedSampler(val_dst, shuffle=False)


    if opts.num_copys == 1:
        train_loader = data.DataLoader(train_dst, batch_size=opts.batch_size, shuffle=True, num_workers=2, sampler=sampler)
    else:
        train_loader = data.DataLoader(train_dst, batch_size=opts.batch_size // opts.num_copys, collate_fn=collate_fn2, shuffle=True, num_workers=2,
                                    drop_last=True, sampler=sampler)

    val_loader = data.DataLoader(val_dst, batch_size=opts.val_batch_size, shuffle=True, num_workers=8, sampler=val_sampler)
    print("Dataset: %s, Train set: %d, Val set: %d" % (opts.dataset, len(train_dst), len(val_dst)))
    model_map = {
        'deeplabv3_resnet50': network.deeplabv3_resnet50,
        'deeplabv3plus_resnet50': network.deeplabv3plus_resnet50,
        'deeplabv3_resnet101': network.deeplabv3_resnet101,
        'deeplabv3plus_resnet101': network.deeplabv3plus_resnet101,
        'deeplabv3_mobilenet': network.deeplabv3_mobilenet,
        'deeplabv3plus_mobilenet': network.deeplabv3plus_mobilenet
    }

    model = model_map[opts.model](num_classes=opts.num_classes, output_stride=opts.output_stride)
    model = nn.parallel.DistributedDataParallel(model)
    
    if opts.separable_conv and 'plus' in opts.model:
        network.convert_to_separable_conv(model.classifier)
    utils.set_bn_momentum(model.backbone, momentum=0.01)
    # model = model.to(device)
    # print(model)

    # Set up metrics
    metrics = StreamSegMetrics(opts.num_classes)

    # Set up optimizer
    optimizer = torch.optim.SGD(params=[
        {'params': model.backbone.parameters(), 'lr': 0.1 * opts.lr},
        {'params': model.classifier.parameters(), 'lr': opts.lr},
    ], lr=opts.lr, momentum=0.9, weight_decay=opts.weight_decay)
    # optimizer = torch.optim.SGD(params=model.parameters(), lr=opts.lr, momentum=0.9, weight_decay=opts.weight_decay)
    # torch.optim.lr_scheduler.StepLR(optimizer, step_size=opts.lr_decay_step, gamma=opts.lr_decay_factor)
    if opts.lr_policy == 'poly':
        scheduler = utils.PolyLR(optimizer, opts.total_itrs, power=0.9)
    elif opts.lr_policy == 'step':
        scheduler = torch.optim.lr_scheduler.StepLR(
            optimizer, step_size=opts.step_size, gamma=0.1)

    # Set up criterion
    # criterion = utils.get_loss(opts.loss_type)
    if opts.loss_type == 'focal_loss':
        criterion = utils.FocalLoss(ignore_index=255, size_average=True)
    elif opts.loss_type == 'cross_entropy':
        criterion = nn.CrossEntropyLoss(ignore_index=255, reduction='mean')

    # Criterion = ssp_loss()
    # Criterion = Mixed_Loss()

    if opts.pgc_mode == 1:
        use_pgc = [0]
    elif opts.pgc_mode == 2:
        use_pgc = [1]
    elif opts.pgc_mode == 3:
        use_pgc = [2]
    else:
        use_pgc = [0, 1, 2]

    print("pgc mode is:", use_pgc)

    Criterion = PGC_loss(use_pgc = use_pgc)

    def save_ckpt(path):
        """ save current model
        """
        torch.save({
            "cur_itrs": cur_itrs,
            "model_state": model.state_dict(),
            "optimizer_state": optimizer.state_dict(),
            "scheduler_state": scheduler.state_dict(),
            "best_score": best_score,
        }, path)
        print("Model saved as %s" % path)

    utils.mkdir('checkpoints')
    # Restore
    best_score = 0.0
    cur_itrs = 0
    cur_epochs = 0
    if opts.ckpt is not None and os.path.isfile(opts.ckpt):
        checkpoint = torch.load(opts.ckpt)
        model.load_state_dict(checkpoint["model_state"])
        if opts.continue_training:
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            scheduler.load_state_dict(checkpoint["scheduler_state"])
            cur_itrs = checkpoint["cur_itrs"]
            best_score = checkpoint['best_score']
            print("Training state restored from %s" % opts.ckpt)
        print("Model restored from %s" % opts.ckpt)
        del checkpoint  # free memory
    else:
        print("[!] Retrain")
    # try:
    #     # model = nn.DataParallel(model, device_ids=Device)
    #     model = nn.DataParallel(model)
    # except:
    #     print("Device is:", Device)
    #     exit(0)
    model.to(device)

    # ==========   Train Loop   ==========#
    vis_sample_id = np.random.randint(0, len(val_loader), opts.vis_num_samples,
                                    np.int32) if opts.enable_vis else None  # sample idxs for visualization
    denorm = utils.Denormalize(mean=[0.485, 0.456, 0.406], std=[
        0.229, 0.224, 0.225])  # denormalization for ori images

    if opts.test_only:
        model.eval()
        val_score, ret_samples = validate(
            opts=opts, model=model, loader=val_loader, device=device, metrics=metrics, ret_samples_ids=vis_sample_id)
        print(metrics.to_str(val_score))
        return

    interval_loss = 0
    total_mse = 0
    total_ce = 0
    total_exce = 0
    total_sym_ce = 0
    total_pgc = 0
    alpha = opts.alpha
    beta = opts.beta

    while cur_itrs < opts.total_itrs:
        print(cur_itrs)
        # =====  Train  =====
        model.train()
        cur_epochs += 1
        for sample in train_loader:
            if opts.num_copys > 1:
                images, labels, overlap, flips = sample
            elif opts.num_copys == 1:
                images, labels = sample
            cur_itrs += 1

            images = images.to(device, dtype=torch.float32)
            labels = labels.to(device, dtype=torch.long)

            optimizer.zero_grad()
            image1, image2 = images[::2], images[1::2]
            
            output1 = model(image1)
            output2 = model(image2)

            # outputs = {'seg': torch.cat([output1['seg'], output2['seg']]),
            # 'embedding':[torch.cat((output1['embedding'][0], output2['embedding'][0]), dim=0),
            #  torch.cat((output1['embedding'][1], output2['embedding'][1]), dim=0)]}
            outputs = []
            for f1, f2 in zip(output1, output2):
                outputs.append([f1, f2])

            mse, sym_ce, mid_mse, mid_ce, mid_l1, ce = Criterion(outputs, overlap, flips, labels)
            loss = beta * sym_ce + ce
            pgc_loss = sum(mid_mse)
            loss += alpha * pgc_loss
            #			else:
            #				with torch.no_grad():
            #					mse, ce_1_2, ce_2_1, sym_ce = Criterion(outputs, overlap, flips)

            loss.backward()
            optimizer.step()

            np_loss = loss.detach().cpu().numpy()
            mse = mse.detach().cpu().numpy().item()
            sym_ce = sym_ce.detach().cpu().numpy().item()
            ce = ce.detach().cpu().numpy().item()
            pgc = pgc_loss.detach().cpu().numpy().item()
            interval_loss += np_loss
            total_mse += mse
            total_sym_ce += sym_ce
            total_ce += ce
            total_pgc += pgc

            if cur_itrs % 10 == 0:
                interval_loss = interval_loss / 10
                total_mse, total_ce = total_mse / 10, total_ce / 10
                total_sym_ce /= 10

                print("Epoch %d, Itrs %d/%d, Loss=%f, pgc=%f, mse=%f,  sym_ce=%f, ce=%f" %
                    (cur_epochs, cur_itrs, opts.total_itrs, interval_loss, total_pgc, total_mse, total_sym_ce, total_ce))
                interval_loss = 0.0
                total_mse = 0
                total_ce = 0
                total_sym_ce = 0
                total_pgc = 0

            if cur_itrs % opts.val_interval == 0:
                save_ckpt('checkpoints/latest_%s_%s_os%d.pth' %
                        (opts.model, opts.dataset, opts.output_stride))
                print("validation...")
                model.eval()
                val_score, ret_samples = validate(
                    opts=opts, model=model, loader=val_loader, device=device, metrics=metrics, ret_samples_ids=vis_sample_id)
                print(metrics.to_str(val_score))
                if val_score['Mean IoU'] > best_score:  # save best model
                    best_score = val_score['Mean IoU']
                    save_ckpt('checkpoints/best_%s_%s_os%d.pth' % (opts.model, opts.dataset, opts.output_stride))

                    for k, (img, target, lbl) in enumerate(ret_samples):
                        img = (denorm(img) * 255).astype(np.uint8)
                        target = train_dst.decode_target(
                            target).transpose(2, 0, 1).astype(np.uint8)
                        lbl = train_dst.decode_target(
                            lbl).transpose(2, 0, 1).astype(np.uint8)
                        concat_img = np.concatenate(
                            (img, target, lbl), axis=2)  # concat along width
                model.train()
            scheduler.step()


if __name__ == '__main__':
    main()
