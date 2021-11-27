import copy
import json
import time
from collections import defaultdict
from datetime import datetime

import torch
from torch.utils.data import DataLoader
import os

from scripts import DatasetSplit, zero_rates, ternary_convert, current_learning_rate, rec_w
import logging
import torch.nn as nn
import numpy as np

from utils.misc import AverageMeter, Timer
from pprint import pprint
from configs import *
import configs
import io


# train_acc, train_loss = [], []
# test_acc, test_loss = [], []
# train_time = []
# comp_rate = []
# update_zero_rate = []


class Client(object):
    def __init__(self, config, dataset=None, model=None, client_idx=None):
        self.loss_func = nn.CrossEntropyLoss()
        self.model = model
        self.local_train_dataset = dataloader(dataset, config['local_bs'])
        self.client_idx = client_idx
        self.device = config['device']
        self.ternary_convert = config['tnt_upload']

    def train(self, config):
        net = self.model
        net.train()
        optimizer = configs.optimizer(config, net.parameters())

        total_timer = Timer()
        timer = Timer()

        total_timer.tick()
        client_batch = []
        logging.info(f'Client {self.client_idx} is training on GPU {self.device}.')
        client_meters = defaultdict(AverageMeter)
        client_ep = {}
        for ep in range(config['local_ep']):
            meters = defaultdict(AverageMeter)
            res = {'ep': ep + 1}
            correct = 0
            for batch_idx, (images, labels) in enumerate(self.local_train_dataset):
                timer.tick()

                images = images.to(config['device'])
                labels = labels.type(torch.LongTensor).to(config['device'])

                net.zero_grad()

                prob = net(images)
                loss = self.loss_func(prob, labels)
                loss.backward()
                optimizer.step()

                pre_labels = prob.data.max(1, keepdim=True)[1]

                correct += pre_labels.eq(labels.data.view_as(pre_labels)).long().cpu().sum()
                training_acc = correct.item() / len(self.local_train_dataset.dataset)
                timer.toc()

                # store results
                meters['loss_total'].update(loss.item(), images.size(0))
                meters['acc'].update(training_acc, images.size(0))
                meters['time'].update(timer.total)

                print(f'Epoch {ep} Client {self.client_idx} '
                      f'Train [{batch_idx + 1}/{len(self.local_train_dataset)}] '
                      f'Total Loss: {meters["loss_total"].avg:.4f} '
                      f'A(CE): {meters["acc"].avg:.2%} '
                      f'({timer.total:.2f}s / {total_timer.total:.2f}s)', end='\r')

            total_timer.toc()
            meters['total_time'].update(total_timer.total)

            for key in meters: res['train_' + key] = meters[key].avg
            client_batch.append(res)

        for item in client_batch:
            for key in item.keys():
                client_meters[str(self.client_idx) + '_' + key].update(item[key])
        for key in client_meters:
            client_ep[key] = client_meters[key].avg

        if config['tnt_upload']:
            w_tnt, local_error = ternary_convert(copy.deepcopy(net))  # transmit tnt error
            return w_tnt, local_error, client_ep

        else:
            return net.state_dict(), client_ep


def test(model, data_test, config):
    model.eval()
    loss_func = nn.CrossEntropyLoss()

    meters = defaultdict(AverageMeter)
    total_timer = Timer()
    timer = Timer()
    total_timer.tick()

    # testing
    testing_loss = 0
    correct = 0
    total = 0
    data_loader = DataLoader(data_test, batch_size=config['bs'])

    logging.info(f'Testing on GPU {config["device"]}.')
    for i, (data, labels) in enumerate(data_loader):
        timer.tick()

        with torch.no_grad():
            data = data.to(config['device'])
            labels = labels.type(torch.LongTensor).to(config['device'])
            log_probs = model(data)

            # sum up batch loss
            testing_loss += loss_func(log_probs, labels)
            # F.cross_entropy(log_probs, labels, reduction='sum')
            # get the index of the max log-probability
            y_pred = log_probs.data.max(1, keepdim=True)[1]
            correct += y_pred.eq(labels.data.view_as(y_pred)).long().cpu().sum()
            total += labels.size(0)
        timer.toc()
        total_timer.toc()

        acc = correct.item() / len(data_loader.dataset)

        # store results
        meters['testing_loss_total'].update(testing_loss, data.size(0))
        meters['testing_acc'].update(acc, data.size(0))
        meters['time'].update(timer.total)

        print(f'Test [{i + 1}/{len(data_loader)}] '
              f'T(loss): {meters["testing_loss_total"].avg:.4f} '
              f'A(CE): {meters["testing_acc"].avg:.2%} '
              f'({timer.total:.2f}s / {total_timer.total:.2f}s)', end='\r')

    return meters

    # # saving best
    # if acc > best_acc:
    #     print('Saving..')
    #     state = {
    #         # 'net': net_g.get_tnt(),  # net_g.get_tnt(),  # 'net':net.get_tnt() for tnt network // net.state_dict()
    #         'net': net_g.get_tnt() if config['tnt_upload'] else net_g.state_dict(),
    #         # net_g.get_tnt(),  # 'net':net.get_tnt() for tnt network // net.state_dict()
    #         'acc': acc * 100.,
    #         'epoch': epoch,
    #     }
    #     if not os.path.isdir('checkpoint'):
    #         os.mkdir('checkpoint')
    #     torch.save(state, './checkpoint/{}.ckpt'.format(config['history']))
    #     best_acc = acc
    #
    # if config['save']:
    #     dict_name = config['history'].split('.')[0]
    #     path = os.path.join('./saved/', '{}/epoch_{}_{}.ckpt'.format(dict_name, epoch, config['history']))
    #     if epoch % 10 == 0:
    #         print('Saving..')
    #         state = {
    #             # 'net': net_g.get_tnt(),
    #             'net': net_g.get_tnt() if config['tnt_upload'] else net_g.state_dict(),
    #             # net_g.get_tnt(),  # 'net':net.get_tnt() for tnt network // net.state_dict()
    #             'acc': acc * 100.,
    #             'epoch': epoch,
    #         }
    #         if not os.path.isdir('./saved/{}'.format(dict_name)):
    #             os.makedirs('./saved/{}'.format(dict_name))
    #         torch.save(state, path)
    #         best_acc = acc
    # return acc, test_loss, best_acc


class Aggregator(object):
    def __init__(self, config):
        self.client_num = config['client_num']
        self.model = configs.arch(config).to(config['device'])
        self.model_name = config['model_name']
        self.zero_rate = False

    def inited_model(self):
        return self.model

    def client_model(self, model):
        cli_model = {}
        for idx in range(self.client_num):
            cli_model[str(idx)] = copy.deepcopy(model)
        return cli_model

    def params_aggregation(self, parma_dict):
        w = list(parma_dict.values())
        w_avg = copy.deepcopy(w[0])

        for k in w_avg.keys():
            for i in range(1, len(w)):
                w_avg[k] += w[i][k]
            w_avg[k] = torch.div(w_avg[k], float(len(w)))

        return w_avg


def prepare_dataset(config):
    logging.info('Creating Datasets')
    train_dataset = configs.dataset(config, filename='train.txt', transform_mode='train')
    logging.info(f'Number of Train data: {len(train_dataset)}')
    test_dataset = configs.dataset(config, filename='test.txt', transform_mode='test')
    logging.info(f'Number of Test data: {len(test_dataset)}')

    return train_dataset, test_dataset


def clients_group(config, model):
    m = max(int(config['client_frac'] * config['client_num']), 1)
    users_index = np.random.choice(range(config['client_num']), m, replace=False)

    train_dataset, test_dataset = prepare_dataset(config)
    print(len(test_dataset.targets))

    config['train_set'] = train_dataset
    config['test_set'] = test_dataset

    client_group = {}
    for idx in users_index:
        client = Client(config=config,
                        dataset=train_dataset[idx],
                        model=copy.deepcopy(model),
                        client_idx=idx)
        client_group[idx] = client

    return client_group


def average(lst):
    return sum(lst) / len(lst)


def main_tnt_upload(config):
    logdir = config['logdir']
    assert logdir != '', 'please input logdir'

    pprint(config)

    os.makedirs(f'{logdir}/models', exist_ok=True)
    os.makedirs(f'{logdir}/optims', exist_ok=True)
    os.makedirs(f'{logdir}/outputs', exist_ok=True)
    json.dump(config, open(f'{logdir}/config.json', 'w+'), indent=4, sort_keys=True)

    aggregator = Aggregator(config)

    print('==> Building model..')
    inited_mode = aggregator.inited_model()
    print(inited_mode)
    print('Deliver model to clients')
    client_net = aggregator.client_model(inited_mode)

    print('Init Clients')
    client_group = clients_group(config)
    current_lr = config['current_lr']

    for epoch in range(config['epochs']):
        start_time = time.time()
        client_upload = {}
        client_local = {}
        acc_locals_train = {}
        loss_locals_train = []
        local_zero_rates = []

        print(f'\n | Global Training Round: {epoch} Training {config["history"]}|\n')

        # training
        for idx in client_group.keys():
            ter_params, qua_error, res = client_group[idx].train(config,
                                                                 net=client_net[str(idx)].to(config['device']))
            client_local[str(idx)] = copy.deepcopy(qua_error)
            client_upload[str(idx)] = copy.deepcopy(ter_params)
            z_r = zero_rates(ter_params)
            local_zero_rates.append(z_r)
            logging.info(f'Client {idx} zero rate {z_r:.2%}')

            # recording local training info
            acc_locals_train[str(idx)] = copy.deepcopy(res[-1]['train_acc'])
            loss_locals_train.append(copy.deepcopy(res[-1]['train_loss_total']))

        elapsed = time.time() - start_time
        train_time.append(elapsed)

        # aggregation in server
        glob_avg = aggregator.params_aggregate(copy.deepcopy(client_upload))

        # update local models
        for idx in client_group.keys():
            client_net[str(idx)] = rec_w(copy.deepcopy(glob_avg),
                                         copy.deepcopy(client_local[str(idx)]),
                                         client_net[str(idx)])

        # client testing
        logging.info(f'\n |Round {epoch} Client Test {config["history"]}|\n')
        client_acc = []
        client_loss = []
        for idx in client_group.keys():
            logging.info(f'Client {idx} Testing on GPU {config["device"]}.')
            testing_res = test(model=client_net[str(idx)],
                               data_test=config['test_set'],
                               config=config)

            test_acc.append(testing_res['testing_acc'])
            test_loss.append(testing_res['testing_loss_total'])

            client_acc.append(testing_res['testing_acc'].avg)
            client_loss.append(testing_res['testing_loss_total'].avg)
        test_acc.append(sum(client_acc) / len(client_group))
        test_loss.append(sum(client_loss) / len(client_group))

        # training info update
        avg_acc_train = sum(acc_locals_train.values()) / len(acc_locals_train.values())

        train_acc.append(avg_acc_train)

        loss_avg = sum(loss_locals_train) / len(loss_locals_train)
        train_loss.append(loss_avg)
        try:
            temp_zero_rates = sum(local_zero_rates) / len(local_zero_rates)

        except:
            temp_zero_rates = sum(local_zero_rates)
        update_zero_rate.append(temp_zero_rates)

        print(f'Round {epoch} costs time: {elapsed:.2f}s| Train Acc.: {avg_acc_train:.2%}| '
              f'Test Acc.{test_acc[-1]:.2%}| Train loss: {loss_avg:.4f}| Test loss: {test_loss[-1]:.4f}| '
              f'| Up Rate{temp_zero_rates:.3%}')

        current_lr = current_learning_rate(epoch, current_lr, config)

    his_dict = {
        'train_loss': train_loss,
        'train_accuracy': train_acc,
        'test_loss': test_loss,
        'test_correct': test_acc,
        'train_time': train_time,
        'glob_zero_rates': comp_rate,
        'local_zero_rates': update_zero_rate,

    }

    os.makedirs('./his/', exist_ok=True)
    with open('./his/{}.json'.format(config["history"]), 'w+') as f:
        json.dump(his_dict, f, indent=2)


def main_norm_upload(config):
    logdir = config['logdir']
    assert logdir != '', 'please input logdir'

    pprint(config)

    os.makedirs(f'{logdir}/models', exist_ok=True)
    os.makedirs(f'{logdir}/optims', exist_ok=True)
    os.makedirs(f'{logdir}/outputs', exist_ok=True)
    json.dump(config, open(f'{logdir}/config.json', 'w+'), indent=4, sort_keys=True)

    aggregator = Aggregator(config)
    print('==> Building model..')
    inited_model = aggregator.inited_model()
    print(inited_model)

    print('Init Clients')
    client_group = clients_group(config, inited_model)

    round_train_acc = []
    round_train_loss = []
    round_test_acc = []
    round_test_loss = []
    round_time = []
    train_history = []
    test_history = []

    nepochs = config['epochs']
    neval = config['eval_interval']

    best = 0
    curr_metric = 0

    for epoch in range(config['epochs']):
        start_time = time.time()
        client_upload = {}
        train_acc_total = []
        train_loss_total = []

        print(f'\n | Global Training Round: {epoch} Training {config["history"]}|\n')

        # training
        for idx in client_group.keys():
            w_, client_ep = client_group[idx].train(config)
            client_upload[idx] = copy.deepcopy(w_)

            train_history.append(client_ep)

            # recording local training info
            train_acc_total.append(client_ep[str(idx) + '_train_acc'])
            train_loss_total.append(client_ep[str(idx) + '_train_loss_total'])

        round_train_acc.append(average(train_acc_total))
        round_train_loss.append(average(train_loss_total))

        json.dump(train_history, open(f'{logdir}/train_history.json', 'w+'), indent=True, sort_keys=True)

        # aggregation in server
        glob_avg = aggregator.params_aggregation(copy.deepcopy(client_upload))
        inited_model.load_state_dict(glob_avg)

        # update local models
        for idx in client_group.keys():
            client_group[idx].model.load_state_dict(glob_avg)

        # ====model testing===

        # eval_now = (epoch + 1) == nepochs or (neval != 0 and (ep + 1) % neval == 0)
        # if eval_now:
        print(f'\n |Round {epoch} Global Test {config["history"]}|\n')
        testing_res = test(model=inited_model,
                           data_test=config['test_set'],
                           config=config)

        curr_metric = testing_res['testing_acc']

        round_test_acc.append(testing_res['testing_acc'])
        round_test_loss.append(testing_res['testing_loss_total'])


        test_history.append(testing_res)

        if len(test_history) != 0:
            json.dump(test_history, open(f'{logdir}/test_history.json', 'w+'), indent=True, sort_keys=True)

        elapsed = time.time() - start_time
        round_time.append(elapsed)

        print(f'Round {epoch} costs time: {elapsed:.2f}s|'
              f'Train Acc.: {round_train_acc[-1]:.2%}| Test Acc.{round_test_acc[-1].avg:.2%}| '
              f'Train loss: {round_train_loss[-1]:.4f}| Test loss: {round_test_loss[-1].avg:.4f}| ')

        modelsd = inited_model.state_dict()
        # optimsd = optimizer.state_dict()
        # io.fast_save(modelsd, f'{logdir}/models/last.pth')
        # io.fast_save(optimsd, f'{logdir}/optims/last.pth')
        save_now = config['save_interval'] != 0 and (epoch + 1) % config['save_interval'] == 0
        if save_now:
            io.fast_save(modelsd, f'{logdir}/models/ep{epoch + 1}.pth')
            # io.fast_save(optimsd, f'{logdir}/optims/ep{ep + 1}.pth')
            # io.fast_save(train_outputs, f'{logdir}/outputs/train_ep{ep + 1}.pth')

        if best < curr_metric:
            best = curr_metric
            io.fast_save(modelsd, f'{logdir}/models/best.pth')

    modelsd = inited_model.state_dict()
    io.fast_save(modelsd, f'{logdir}/models/last.pth')
    total_time = time.time() - start_time
    io.join_save_queue()
    logging.info(f'Training End at {datetime.today().strftime("%Y-%m-%d %H:%M:%S")}')
    logging.info(f'Total time used: {total_time / (60 * 60):.2f} hours')
    logging.info(f'Best mAP: {best:.6f}')
    logging.info(f'Done: {logdir}')


