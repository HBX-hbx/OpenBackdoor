from openbackdoor.victims import Victim
from openbackdoor.utils import logger, evaluate_classification
from openbackdoor.data import get_dataloader, wrap_dataset
from transformers import  AdamW, get_linear_schedule_with_warmup
import torch
from torch.utils.data import DataLoader
from datetime import datetime
import torch.nn as nn
import os
from tqdm import tqdm
from typing import *
from sklearn.decomposition import PCA
from umap import UMAP
import numpy as np
import pandas as pd
from matplotlib import pyplot as plt

class Trainer(object):
    r"""
    Basic clean trainer 

    Args:
        name (:obj:`str`, optional): name of the trainer. Default to "Base".
        lr (:obj:`float`, optional): learning rate. Default to 2e-5.
        weight_decay (:obj:`float`, optional): weight decay. Default to 0.
        epochs (:obj:`int`, optional): number of epochs. Default to 10.
        batch_size (:obj:`int`, optional): batch size. Default to 4.
        gradient_accumulation_steps (:obj:`int`, optional): gradient accumulation steps. Default to 1.
        max_grad_norm (:obj:`float`, optional): max gradient norm. Default to 1.0.
        warm_up_epochs (:obj:`int`, optional): warm up epochs. Default to 3.
        ckpt (:obj:`str`, optional): checkpoint name. Can be "best" or "last". Default to "best".
        save_path (:obj:`str`, optional): path to save the model. Default to "./models/checkpoints".
        loss_function (:obj:`str`, optional): loss function. Default to "ce".
        visualize (:obj:`bool`, optional): visualize the representations. Default to False.
    """
    def __init__(
        self, 
        name: Optional[str] = "Base",
        lr: Optional[float] = 2e-5,
        weight_decay: Optional[float] = 0.,
        epochs: Optional[int] = 10,
        batch_size: Optional[int] = 4,
        gradient_accumulation_steps: Optional[int] = 1,
        max_grad_norm: Optional[float] = 1.0,
        warm_up_epochs: Optional[int] = 3,
        ckpt: Optional[str] = "best",
        save_path: Optional[str] = "./models/checkpoints",
        loss_function: Optional[str] = "ce",
        visualize: Optional[bool] = False,
        **kwargs):

        self.name = name
        self.lr = lr
        self.weight_decay = weight_decay
        self.epochs = epochs
        self.batch_size = batch_size
        self.warm_up_epochs = warm_up_epochs
        self.ckpt = ckpt
        timestamp = int(datetime.now().timestamp())
        self.save_path = os.path.join(save_path, str(timestamp))
        os.mkdir(self.save_path)
        self.gradient_accumulation_steps = gradient_accumulation_steps
        self.max_grad_norm = max_grad_norm
        if loss_function == "ce":
            self.loss_function = nn.CrossEntropyLoss()
        self.visualize_flag = visualize
    
    def register(self, model: Victim, dataloader, metrics):
        r"""
        Register model, dataloader and optimizer
        """
        self.model = model
        self.metrics = metrics
        self.main_metric = self.metrics[0]
        self.split_names = dataloader.keys()
        self.model.train()
        self.model.zero_grad()
        no_decay = ['bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in self.model.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': self.weight_decay},
            {'params': [p for n, p in self.model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
            ]
        self.optimizer = AdamW(optimizer_grouped_parameters, lr=self.lr)
        train_length = len(dataloader["train"])
        self.scheduler = get_linear_schedule_with_warmup(self.optimizer,
                                                    num_warmup_steps=self.warm_up_epochs * train_length,
                                                    num_training_steps=(self.warm_up_epochs+self.epochs) * train_length)
        # Train
        logger.info("***** Training *****")
        logger.info("  Num Epochs = %d", self.epochs)
        logger.info("  Instantaneous batch size per GPU = %d", self.batch_size)
        logger.info("  Gradient Accumulation steps = %d", self.gradient_accumulation_steps)
        logger.info("  Total optimization steps = %d", self.epochs * train_length)

    def train_one_epoch(self, epoch: int, epoch_iterator):
        """
        Train one epoch function.

        Args:
            epoch (:obj:`int`): current epoch.
            epoch_iterator (:obj:`torch.utils.data.DataLoader`): dataloader for training.
        
        Returns:
            :obj:`float`: average loss of the epoch.
        """
        self.model.train()
        total_loss = 0
        for step, batch in enumerate(epoch_iterator):
            batch_inputs, batch_labels = self.model.process(batch)
            output = self.model(batch_inputs)
            logits = output.logits
            loss = self.loss_function(logits, batch_labels)
            
            if self.gradient_accumulation_steps > 1:
                loss = loss / self.gradient_accumulation_steps
            else:
                loss.backward()
            
            if (step + 1) % self.gradient_accumulation_steps == 0:
                nn.utils.clip_grad_norm_(self.model.parameters(), self.max_grad_norm)
                self.optimizer.step()
                self.scheduler.step()
                total_loss += loss.item()
                self.model.zero_grad()

        avg_loss = total_loss / len(epoch_iterator)
        return avg_loss

    def train(self, model: Victim, dataset, metrics: Optional[List[str]] = ["accuracy"], config: Optional[dict] = None):
        """
        Train the model.

        Args:
            model (:obj:`Victim`): victim model.
            dataset (:obj:`Dict`): dataset.
            metrics (:obj:`List[str]`, optional): list of metrics. Default to ["accuracy"].
            config (:obj:`Dict`): config. Default to None
        Returns:
            :obj:`Victim`: trained model.
        """

        dataloader = wrap_dataset(dataset, self.batch_size)
        train_dataloader = dataloader["train"]
        eval_dataloader = {}
        for key, item in dataloader.items():
            if key.split("-")[0] == "dev":
                eval_dataloader[key] = dataloader[key]
        self.register(model, dataloader, metrics)
        
        best_dev_score = 0

        if self.visualize_flag:
            poison_method = config["attacker"]["poisoner"]["name"]
            poison_rate = config["attacker"]["poisoner"]["poison_rate"]
            poison_setting = "clean" if config["attacker"]["poisoner"]["label_consistency"] else "dirty"
            self.COLOR = ['deepskyblue', 'salmon', 'palegreen', 'violet', 'paleturquoise', 
                            'green', 'mediumpurple', 'gold', 'royalblue']
            dataset = get_dataloader(dataset['train'], batch_size=32, shuffle=False)

            self.hidden_state, self.labels, self.poison_labels = self.compute_hidden(model, dataset)

        for epoch in range(self.epochs):
            epoch_iterator = tqdm(train_dataloader, desc="Iteration")
            epoch_loss = self.train_one_epoch(epoch, epoch_iterator)
            logger.info('Epoch: {}, avg loss: {}'.format(epoch+1, epoch_loss))
            dev_results, dev_score = self.evaluate(self.model, eval_dataloader, self.metrics)

            if self.visualize_flag:
                hidden_state, _, _ = self.compute_hidden(model, dataset)
                self.hidden_state.extend(hidden_state)

            if dev_score > best_dev_score:
                best_dev_score = dev_score
                if self.ckpt == 'best':
                    torch.save(self.model.state_dict(), self.model_checkpoint(self.ckpt))

        if self.visualize_flag:
            hidden_path = os.path.join('./hidden_states', 
                            poison_setting, poison_method, str(poison_rate))
            os.makedirs(hidden_path, exist_ok=True)
            np.save(os.path.join(hidden_path, 'hidden_states.npy'), np.array(self.hidden_state))
            np.save(os.path.join(hidden_path, 'labels.npy'), np.array(self.labels))
            np.save(os.path.join(hidden_path, 'poison_labels.npy'), np.array(self.poison_labels))
            self.visualize(self.hidden_state, self.labels, self.poison_labels, 
                            fig_basepath=os.path.join('./visualization', poison_setting, poison_method, str(poison_rate)))

        if self.ckpt == 'last':
            torch.save(self.model.state_dict(), self.model_checkpoint(self.ckpt))

        logger.info("Training finished.")
        state_dict = torch.load(self.model_checkpoint(self.ckpt))
        self.model.load_state_dict(state_dict)
        # test_score = self.evaluate_all("test")
        return self.model
   
    
    def evaluate(self, model, eval_dataloader, metrics):
        """
        Evaluate the model.

        Args:
            model (:obj:`Victim`): victim model.
            eval_dataloader (:obj:`torch.utils.data.DataLoader`): dataloader for evaluation.
            metrics (:obj:`List[str]`, optional): list of metrics. Default to ["accuracy"].

        Returns:
            results (:obj:`Dict`): evaluation results.
            dev_score (:obj:`float`): dev score.
        """
        results, dev_score = evaluate_classification(model, eval_dataloader, metrics)
        return results, dev_score
    
    def compute_hidden(self, model: Victim, dataloader: DataLoader):
        """
        Prepare the hidden states, ground-truth labels, and poison_labels of the dataset for visualization.

        Args:
            model (:obj:`Victim`): victim model.
            dataloader (:obj:`torch.utils.data.DataLoader`): non-shuffled dataloader for train set.

        Returns:
            hidden_state (:obj:`List`): hidden state of the training data.
            labels (:obj:`List`): ground-truth label of the training data.
            poison_labels (:obj:`List`): poison label of the poisoned training data.
        """
        logger.info('***** Computing hidden hidden_state *****')
        model.eval()
        # get hidden state of PLMs
        hidden_state = []
        labels = []
        poison_labels = []
        for batch in tqdm(dataloader):
            text, label, poison_label = batch['text'], batch['label'], batch['poison_label']
            labels.extend(label)
            poison_labels.extend(poison_label)
            batch_inputs, _ = model.process(batch)
            output = model(batch_inputs)
            hidden_state = output.hidden_states[-1] # we only use the hidden state of the last layer
            try: # bert
                pooler_output = getattr(model.plm, model.model_name.split('-')[0]).pooler(hidden_state)
            except: # RobertaForSequenceClassification has no pooler
                dropout = model.plm.classifier.dropout
                dense = model.plm.classifier.dense
                try:
                    activation = model.plm.activation
                except:
                    activation = torch.nn.Tanh()
                pooler_output = activation(dense(dropout(hidden_state[:, 0, :])))
            hidden_state.extend(pooler_output.detach().cpu().tolist())
        model.train()
        return hidden_state, labels, poison_labels

    def visualize(self, hidden_state: List, labels: List, poison_labels: List, fig_basepath: Optional[str]="./visualization", fig_title: Optional[str]="vis"):
        """
        Visualize the latent representation of the victim model on the poisoned dataset and save to 'fig_basepath'.

        Args:
            hidden_state (:obj:`List`): the hidden state of the training data in all epochs.
            labels (:obj:`List`): ground-truth label of the training data.
            poison_labels (:obj:`List`): poison label of the poisoned training data.
            fig_basepath (:obj:`str`, optional): dir path to save the model. Default to "./visualization".
            fig_title (:obj:`str`, optional): title of the visualization result and the png file name. Default to "vis".
        """
        logger.info('***** Visulizaing *****')
        
        # dimension reduction
        dataset_len = len(labels)
        epochs = int(len(hidden_state) / dataset_len)

        hidden_state = np.array(hidden_state)
        
        
        labels = np.array(labels)
        poison_labels = np.array(poison_labels, dtype=np.int64)
        # plot normal samples
        poison_idx = np.where(poison_labels==np.ones_like(poison_labels))[0]
        num_classes = len(set(labels))
        
        for epoch in range(epochs):
            fig_title = f'epoch_{epoch}'
            # visualization
            representation = hidden_state[epoch*dataset_len : (epoch+1)*dataset_len]
            pca = PCA(n_components=50, 
                        random_state=42,
                        )
            umap = UMAP( n_neighbors=100, 
                            min_dist=0.5,
                            n_components=2,
                            random_state=42,
                            transform_seed=42,
                            )
            embedding_pca = pca.fit_transform(representation)
            embedding = umap.fit(embedding_pca).embedding_
            embedding = pd.DataFrame(embedding)
            for label in range(num_classes):
                idx = np.where(labels==int(label)*np.ones_like(labels))[0]
                idx = list(set(idx) ^ set(poison_idx))
                plt.scatter(embedding.iloc[idx,0], embedding.iloc[idx,1], c=self.COLOR[label], s=1, label=label)
            
            #plot poison samples
            plt.scatter(embedding.iloc[poison_idx,0], embedding.iloc[poison_idx,1], s=1, c='gray', label='poison')
            plt.grid()
            # ax = plt.gca()
            # ax.set_facecolor('lavender')
            plt.legend()
            plt.title(f'{fig_title}')
            os.makedirs(fig_basepath, exist_ok=True)
            plt.savefig(os.path.join(fig_basepath, f'{fig_title}.png'))
            print('saving png to', os.path.join(fig_basepath, f'{fig_title}.png'))
            plt.close()


    
    def model_checkpoint(self, ckpt: str):
        return os.path.join(self.save_path, f'{ckpt}.ckpt')
