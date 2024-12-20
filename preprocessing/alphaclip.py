import os

import alpha_clip
import numpy as np
import torch
from PIL import Image
from torchvision import transforms

import configuration as c

config = c.load_yaml_config("config.yaml")

mask_transform = transforms.Compose(
    [
        transforms.ToTensor(),
        transforms.Resize((224, 224)),  # change to (336,336) when using ViT-L/14@336px
        transforms.Normalize(0.5, 0.26),
    ]
)


class AlphaCLIPDownloader:
    models = {
        "ViT-B/16": "https://download.openxlab.org.cn/models/SunzeY/AlphaCLIP/weight/clip_b16_grit1m_fultune_8xe.pth",
        "ViT-L/16": "https://download.openxlab.org.cn/models/SunzeY/AlphaCLIP/weight/clip_l16_grit1m_fultune_8xe.pth",
    }

    @staticmethod
    def download(model, dir):
        checkpoint_name = AlphaCLIPDownloader.models[model].split("/")[-1]
        checkpoint_path = os.path.join(dir, checkpoint_name)

        if not os.path.exists(checkpoint_path):
            print("Downloading model...")
            import urllib.request
            
            # create download directory if it does not exist
            os.makedirs(dir, exist_ok=True)

            urllib.request.urlretrieve(AlphaCLIPDownloader.models[model], checkpoint_path)

        else:
            print("Model has already been downloaded.")

        return checkpoint_path


class AlphaCLIPEncoder:
    def __init__(self, alphaclip_config: c.AlphaCLIPConfig):
        self.config = alphaclip_config

        checkpoint_path = AlphaCLIPDownloader.download(
            self.config.model, self.config.checkpoint_dir
        )

        self.device = "cuda" if torch.cuda.is_available() else "cpu"

        self.alphaclip, self.preprocess = alpha_clip.load(
            self.config.model, alpha_vision_ckpt_pth=checkpoint_path, device=self.device
        )

    def get_visual_embedding(self, image: Image.Image, mask: np.ndarray, mask_only=False):
        binary_mask = mask == 255

        if mask_only:
            image = np.array(image)
            binary_mask = binary_mask.astype(np.uint8)

            image = image * binary_mask[:, :, None]
            image = transforms.ToTensor()(image).half().to(self.device)
            t_binary_mask = torch.tensor(binary_mask).half().cuda().unsqueeze(dim=0)
            image = image * t_binary_mask

            image = Image.fromarray(
                (image.squeeze(0).cpu().numpy().transpose(1, 2, 0) * 255).astype(np.uint8)
            )

        alpha = mask_transform((binary_mask * 255).astype(np.uint8))
        alpha = alpha.half().cuda().unsqueeze(dim=0)

        image = self.preprocess(image).unsqueeze(0).half().to(self.device)

        with torch.no_grad():
            image_features = self.alphaclip.visual(image, alpha).squeeze(0)

        return image_features / image_features.norm(dim=-1, keepdim=True)


if __name__ == "__main__":
    img_name = "11709607_652f25a747_o.jpg"
    image_path = os.path.join(config.dataset.image_dir, img_name)
    mask_path = os.path.join(config.dataset.mask_dir, img_name.split(".")[0], "mask_5.png")

    encoder = AlphaCLIPEncoder(config.alphaclip)
    image = Image.open(image_path)
    mask = np.array(Image.open(mask_path))

    visual_embedding = encoder.get_visual_embedding(image, mask)
    visual_embedding_2 = encoder.get_visual_embedding(image, mask, mask_only=True)

    print(visual_embedding)
    print(visual_embedding_2)
