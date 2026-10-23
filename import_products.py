# import_products.py
import os
import csv
import random
import django
from django.core.files import File
from django.conf import settings
from PIL import Image
import io

# ============================================
# ✅ YOUR PROJECT NAME: 'project'
# ============================================
PROJECT_NAME = 'project'

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', f'{PROJECT_NAME}.settings')

# Initialize Django
django.setup()

from store.models import Product, Category

def create_default_image():
    """Create a default product image if no images exist"""
    products_dir = os.path.join(settings.MEDIA_ROOT, 'products')
    os.makedirs(products_dir, exist_ok=True)
    
    default_path = os.path.join(products_dir, 'default_product.jpg')
    
    if not os.path.exists(default_path):
        # Create a simple placeholder image
        img = Image.new('RGB', (500, 500), color=(144, 188, 121))
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='JPEG', quality=90)
        img_byte_arr.seek(0)
        
        with open(default_path, 'wb') as f:
            f.write(img_byte_arr.getvalue())
        print(f"✅ Created default image: {default_path}")
    
    return default_path

def get_random_image():
    """Get a random image from the media/products folder"""
    products_dir = os.path.join(settings.MEDIA_ROOT, 'products')
    
    if not os.path.exists(products_dir):
        print(f"❌ Products directory not found: {products_dir}")
        return None
    
    image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
    images = []
    
    for file in os.listdir(products_dir):
        file_path = os.path.join(products_dir, file)
        if os.path.isfile(file_path):
            if any(file.lower().endswith(ext) for ext in image_extensions):
                images.append(file)
    
    if not images:
        # Create default image
        default_path = create_default_image()
        return os.path.basename(default_path)
    
    return random.choice(images)

def import_products_from_csv(csv_file):
    """Import products from CSV - Update if exists, create if new"""
    
    if not os.path.exists(csv_file):
        print(f"❌ CSV file not found: {csv_file}")
        return
    
    # Create default image if needed
    create_default_image()
    
    with open(csv_file, 'r', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        
        print(f"📋 Columns found: {', '.join(reader.fieldnames)}")
        
        products_created = 0
        products_updated = 0
        products_failed = 0
        
        products_dir = os.path.join(settings.MEDIA_ROOT, 'products')
        all_images = []
        if os.path.exists(products_dir):
            image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
            all_images = [f for f in os.listdir(products_dir) 
                         if os.path.isfile(os.path.join(products_dir, f))
                         and any(f.lower().endswith(ext) for ext in image_extensions)]
        
        print(f"📸 Found {len(all_images)} images in products folder")
        
        for row in reader:
            try:
                # Get or create category
                category_name = row.get('category', 'General')
                category, _ = Category.objects.get_or_create(name=category_name)
                
                # Check if product already exists
                existing_product = Product.objects.filter(name=row['name']).first()
                
                if existing_product:
                    # ✅ UPDATE existing product
                    existing_product.description = row['description']
                    existing_product.price = float(row['price'])
                    existing_product.compare_price = float(row['compare_price']) if row.get('compare_price') else None
                    existing_product.stock = int(row['stock']) if row.get('stock') else 0
                    existing_product.is_active = row.get('is_active', 'True').lower() in ['true', '1', 'yes']
                    existing_product.category = category
                    existing_product.save()
                    
                    print(f"🔄 Updated existing product: {existing_product.name}")
                    products_updated += 1
                    
                    # Update image if available
                    if all_images:
                        random_image = random.choice(all_images)
                        image_path = os.path.join(products_dir, random_image)
                        
                        if os.path.exists(image_path):
                            with open(image_path, 'rb') as f:
                                existing_product.image.save(random_image, File(f), save=True)
                            print(f"   📸 Updated image: {random_image}")
                            if random_image in all_images:
                                all_images.remove(random_image)
                        else:
                            print(f"   ⚠️ Image missing: {random_image}")
                    else:
                        # Use default image
                        default_image = os.path.join(products_dir, 'default_product.jpg')
                        if os.path.exists(default_image):
                            with open(default_image, 'rb') as f:
                                existing_product.image.save('default_product.jpg', File(f), save=True)
                            print(f"   📸 Used default image")
                    
                else:
                    # ✅ CREATE new product
                    product = Product.objects.create(
                        name=row['name'],
                        description=row['description'],
                        price=float(row['price']),
                        compare_price=float(row['compare_price']) if row.get('compare_price') else None,
                        stock=int(row['stock']) if row.get('stock') else 0,
                        is_active=row.get('is_active', 'True').lower() in ['true', '1', 'yes'],
                        category=category
                    )
                    
                    print(f"✅ Created new product: {product.name}")
                    products_created += 1
                    
                    # Assign image
                    if all_images:
                        random_image = random.choice(all_images)
                        image_path = os.path.join(products_dir, random_image)
                        
                        if os.path.exists(image_path):
                            with open(image_path, 'rb') as f:
                                product.image.save(random_image, File(f), save=True)
                            print(f"   📸 Image: {random_image}")
                            if random_image in all_images:
                                all_images.remove(random_image)
                        else:
                            print(f"   ⚠️ Image missing: {random_image}")
                    else:
                        # Use default image
                        default_image = os.path.join(products_dir, 'default_product.jpg')
                        if os.path.exists(default_image):
                            with open(default_image, 'rb') as f:
                                product.image.save('default_product.jpg', File(f), save=True)
                            print(f"   📸 Used default image")
                        else:
                            print(f"   📸 No image available")
                
            except Exception as e:
                print(f"❌ Failed to process {row.get('name', 'Unknown')}: {str(e)}")
                products_failed += 1
    
    print(f"\n📊 Summary:")
    print(f"   ✅ Products created: {products_created}")
    print(f"   🔄 Products updated: {products_updated}")
    print(f"   ❌ Products failed: {products_failed}")
    print(f"   🎉 Total products: {Product.objects.count()}")

if __name__ == '__main__':
    csv_file = 'products.csv'
    
    print("🚀 Starting product import (update existing, create new)...")
    print(f"📁 Project: {PROJECT_NAME}")
    print(f"📂 Settings: {PROJECT_NAME}.settings")
    print(f"📁 Media Root: {settings.MEDIA_ROOT}")
    import_products_from_csv(csv_file)
    print("✨ Import complete!")