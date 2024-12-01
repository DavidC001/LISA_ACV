import torch
import torch.nn as nn

from transformers import (
    LlavaForConditionalGeneration,
    LlavaProcessor,
    PreTrainedModel,
    BitsAndBytesConfig,
)
from peft import LoraConfig, get_peft_model

from PIL import Image


class SegAdapter(nn.Module):
    """
    Adapter module from the output of AlphaClip from the segmentation model to the input of the LLava model
    """

    def __init__(self, input_segment_dim: int, llava_embedding_dim: int):
        """
        Adapter module from the output of AlphaClip from the segmentation model to the input of the LLava model

        Args:
            input_segment_dim (int): Dimension of the input segment embeddings
            llava_embedding_dim (int): Dimension of the LLava model's token embeddings
        """
        super().__init__()

        self.linear = nn.Linear(input_segment_dim, llava_embedding_dim)

    def forward(self, segment_embeddings: torch.Tensor):
        """
        Forward pass through the adapter module

        Args:
            segment_embeddings (torch.Tensor): Segment embeddings from the segmentation model with shape (batch_size, num_segments, input_segment_dim)

        Returns:
            llava_input (torch.Tensor): Input tensor for the LLava model with shape (batch_size, seq_length, llava_embedding_dim)
        """
        llava_input = self.linear(segment_embeddings)

        return llava_input


# Define the custom model class
class DynamicVocabLlavaModel(nn.Module):
    def __init__(self, model: PreTrainedModel, processor: LlavaProcessor):
        """
        Custom model class that wraps a pre-trained LLava model and adds functionality to add new tokens to the vocabulary
        and reset the token embeddings to the original state

        Args:
            model (PreTrainedModel): Pre-trained LLava model
            processor (LlavaProcessor): Processor object for the LLava model
            adapter (SegAdapter): Adapter module from the output of AlphaClip from the segmentation model to the input of the LLava model
        """
        super().__init__()
        self.llava_model = model
        self.original_vocab_size = model.config.text_config.vocab_size
        self.original_emb_matrix = model.get_input_embeddings().weight.clone().detach()
        self.tokenizer_vocab_size = processor.tokenizer.vocab_size
        self.processor = processor

    def forward(
        self,
        input_ids: torch.Tensor,
        additional_tokens: torch.Tensor,
        num_generate: int = 1,
        reset_tokens: bool = False,
        **kwargs,
    ):
        """
        Forward pass through the model

        Args:
            input_ids (torch.Tensor): Input tensor containing token IDs with shape (batch_size, seq_length)
            additional_tokens (torch.Tensor): Additional tokens to add to the vocabulary and the model's embedding layer with len batch_size and shape (num_tokens, seg_embedding_dim)
            num_generate (int): Number of tokens to generate
            reset_tokens (bool): Whether to reset the token embeddings to the original state after generating tokens (for gradients during training)
            **kwargs: Additional keyword arguments

        Returns:
            logits (torch.Tensor): Logits for the next tokens computed for the last num_generate tokens in the input sequence
        """
        # Add new tokens to the vocabulary and the model's embedding layer
        self.add_tokens(additional_tokens)

        # Prepare inputs for generation
        inputs = self.llava_model.prepare_inputs_for_generation(
            input_ids=input_ids, **kwargs, cache_position=torch.tensor([0])
        )
        inputs["num_logits_to_keep"] = num_generate
        # Forward pass through the model
        outputs = self.llava_model(
            **inputs,
            return_dict=True,
        )
        logits = outputs.logits

        # Generate next tokens
        next_tokens = torch.argmax(logits, dim=-1)
        input_ids = torch.cat([input_ids, next_tokens], dim=-1)
        generated = self.processor.tokenizer.decode(next_tokens[0])
        # print(f"Tokens: {next_tokens[0]}")
        print(f"Generated: {generated}")

        # Reset the token embeddings to the original state
        if reset_tokens:
            self.reset_tokens()

        return logits

    def add_tokens(self, new_tokens):
        print(
            f"Adding {new_tokens.size(0)} new tokens to the vocabulary of size {self.original_vocab_size}"
        )
        # Get the current embedding layer
        embedding_layer = self.llava_model.get_input_embeddings()
        emb_weights = embedding_layer.weight.clone().detach()
        # Get the current number of tokens and embedding dimension
        old_num_tokens, embedding_dim = embedding_layer.weight.size()
        num_new_tokens = new_tokens.size(0)

        # Create a new embedding matrix with the new size
        new_embedding_weights = (
            torch.cat(
                [
                    emb_weights[: self.tokenizer_vocab_size + 1],
                    new_tokens,
                    emb_weights[self.tokenizer_vocab_size + 1 :],
                ]
            )
            .to(embedding_layer.weight.device)
            .to(embedding_layer.weight.dtype)
        )

        # Resize token embeddings
        self.llava_model.resize_token_embeddings(old_num_tokens + num_new_tokens)

        new_embedding_layer = self.llava_model.get_input_embeddings()
        # Assign the new embedding matrix to the embedding layer
        new_embedding_layer.weight = nn.Parameter(new_embedding_weights)

        # Update the LM head to have the same weights as the embeddings
        output_embedding_layer = self.llava_model.get_output_embeddings()
        output_embedding_layer.weight = new_embedding_layer.weight

    def reset_tokens(self):
        print("Resetting the token embeddings")
        # Resize token embeddings
        self.llava_model.resize_token_embeddings(self.original_vocab_size)
        # Assign the original embedding matrix to the embedding layer
        embedding_layer = self.llava_model.get_input_embeddings()
        embedding_layer.weight = nn.Parameter(
            self.original_emb_matrix.to(embedding_layer.weight.device)
        )
        # Update the LM head to have the same weights as the embeddings
        output_embedding_layer = self.llava_model.get_output_embeddings()
        output_embedding_layer.weight = embedding_layer.weight


class LISA_Model(nn.Module):
    """
    Lisa model to be used during training
    """

    def __init__(
        self, model_name: str, seg_emb_size: int, end_turn_token: str = "<end_of_turn>\n", q4: bool = True, q8: bool = False, device: str = "cuda"
    ):
        """Initialize the LISA model

        Args:
            model_name (str): name of the llava model to load from huggingface
            seg_emb_size (int): size of the segment embeddings coming from the segmentation encoder model
            end_turn_token (str, optional): Token to be added at the end of the generated text. Defaults to "<end_of_turn>\n".
            q4 (bool, optional): Load the model in 4-bit quantization. Defaults to True.
            q8 (bool, optional): Load the model in 8-bit quantization. Defaults to False.
            device (str, optional): Device to run the model on. Defaults to "cuda
            
        """
        super(LISA_Model, self).__init__()
        self.seg_emb_size = seg_emb_size

        # Initialize the processor
        processor = LlavaProcessor.from_pretrained(model_name)
        new_tokens = [f"<SEG_MASK_{i}>" for i in range(1, 500)]
        processor.tokenizer.add_tokens(new_tokens)

        # Configuration for quantization
        assert not (q4 and q8), "Only one of q4 or q8 should be True"
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=q4,
            load_in_8bit=q8,
        )

        # Load the pre-trained LLava model and wrap it with the custom model
        model = LlavaForConditionalGeneration.from_pretrained(
            model_name, quantization_config=bnb_config
        )

        # Apply LoRA to the LLava model
        lora_config = LoraConfig(
            r=16,
            lora_alpha=32,
            target_modules=[
                "q_proj",
                "v_proj",
            ],  # Adjust based on the actual module names
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
        )
        model = get_peft_model(model, lora_config)
        self.adapter = SegAdapter(seg_emb_size, model.config.input_segment_dim())

        self.llava_model = DynamicVocabLlavaModel(model, processor)
        
        self.end_token = end_turn_token
        
        self.device = device

    def forward(
        self,
        texts: list[str],
        images: list[Image.Image],
        labels: list[str],
        pos_mask_embeds: list[torch.Tensor],
        neg_mask_embeds: list[torch.Tensor],
        optimizer: torch.optim.Optimizer,
    ):
        """
        Forward pass through the model

        Args:
            texts (list[str]): List of input text sequences I only the text
            images (list[Image.Image]): List of input images
            labels (list[str]): List of target text sequences for the model to predict I expect only the text that the model should predict
            pos_mask_embeds (list[torch.Tensor]): List of positive mask embeddings with shape (num_pos_masks, seg_emb_size)
            neg_mask_embeds (list[torch.Tensor]): List of negative mask embeddings with shape (num_neg_masks, seg_emb_size)
            optimizer (torch.optim.Optimizer): Optimizer object to update the model's parameters

        Returns:
            Tuple[torch.Tensor]: Logits for the next tokens computed for the last num_generate tokens in the input sequence and the loss
        """
        input_texts = []
        free_token = 1
        new_tokens = []

        # Pass all tokens to the adapter and add the corresponding token lemma to the labels texts
        for i in range(len(pos_mask_embeds)):
            transformed = self.adapter(pos_mask_embeds[i])
            for j in range(transformed.size(0)):
                new_tokens.append(transformed[j])
                labels[i] += f" <SEG_MASK_{free_token}>"
                free_token += 1
            
            # apply the chat template to the texts
            input_texts.append(
                self.llava_model.processor.tokenizer.apply_chat_template(
                    [
                        {"role": "user", "content": f"<image>\n{texts[i]}"},
                        {"role": "assistant", "content": labels[i]},
                    ],
                    tokenize=False,
                    add_generation_prompt=False,
                )
            )
            # get the last token of the text as the end token to be added to the labels
            labels[i] += f" {self.end_token}"

        for i in range(len(neg_mask_embeds)):
            transformed = self.adapter(neg_mask_embeds[i])
            for j in range(transformed.size(0)):
                new_tokens.append(transformed[j])
                
        new_tokens = torch.stack(new_tokens)

        # tokenize the texts
        inputs = self.llava_model.processor(input_texts, images, return_tensors="pt", padding=True).to(self.device)
        labels = self.llava_model.processor(
            labels, [], return_tensors="pt", padding=True, truncation=True
        )
        labels_input_ids = labels["input_ids"]
        labels_mask = labels["attention_mask"]
        
        # Forward pass through the model
        logits = self.llava_model(
            input_ids=inputs["input_ids"],
            additional_tokens=new_tokens,
            num_generate=labels_input_ids.size(1),
            reset_tokens=False,
        )
        
        logits = logits.view(-1, logits.size(-1))
        labels_mask = labels_mask.view(-1).unsqueeze(-1).to(logits.device)
        logits = logits * labels_mask
        
        labels_input_ids = labels_input_ids.view(-1)
        # one-hot encode the labels
        labels = torch.nn.functional.one_hot(labels_input_ids, num_classes=logits.size(-1)).float().to(logits.device)
        
        # Compute the cross-entropy loss between the logits and the labels
        loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, labels, reduction="none")
        
        # backward pass
        optimizer.zero_grad()
        
        loss.backward()
        
        # copy the gradients to the mask embeddings
        emb_grads = self.llava_model.get_input_embeddings().weight.gradself.llava_model.get_input_embeddings().weight.grad
        new_tokens.backward(emb_grads[self.llava_model.tokenizer_vocab_size + 1 : self.llava_model.tokenizer_vocab_size + 1 + new_tokens.size(0)])
        
        optimizer.step()
        
        self.llava_model.reset_tokens()
        
        return logits, loss