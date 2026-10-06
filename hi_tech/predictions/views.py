import os
from django.contrib.auth import login, authenticate
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import render, redirect
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.contrib.auth.forms import UserChangeForm
from ultralytics import YOLO

def _load_prediction_model():
    configured_model_path = os.environ.get('PREDICTION_MODEL_PATH')
    if configured_model_path:
        model_path = configured_model_path
        if not os.path.isabs(model_path):
            model_path = os.path.join(settings.BASE_DIR, model_path)
    else:
        model_path = os.path.join(settings.BASE_DIR, 'static/models/newtrain_train.pt')

    return YOLO(model_path)


model = _load_prediction_model()


def home(request):
    return render(request, 'home.html')

def landing_page(request):
    return render(request, 'landing_page.html')

def predict(request):
    image = request.FILES.get('image')
    if request.method == 'POST' and image:
        # Ensure media directory exists
        os.makedirs(settings.MEDIA_ROOT, exist_ok=True)

        # Save the uploaded image
        fs = FileSystemStorage(location=settings.MEDIA_ROOT, base_url=settings.MEDIA_URL)
        filename = fs.save(image.name, image)
        uploaded_file_url = fs.url(filename)

        # Make the prediction based on the uploaded image
        predicted_class, confidence = predict_plant_disease(os.path.join(settings.MEDIA_ROOT, filename))

        # Store the result in context to send it to the result page
        context = {
            'uploaded_file_url': uploaded_file_url,
            'predicted_class': predicted_class,
            'confidence': confidence,
        }

        return render(request, 'result.html', context)  # Result page to show the result

    return render(request, 'home.html')  # The page to upload the image


def predict_plant_disease(image_path):
    results = model.predict(source=image_path, verbose=False)
    probabilities = results[0].probs
    predicted_class_idx = int(probabilities.top1)
    predicted_class = results[0].names[predicted_class_idx]
    confidence = round(float(probabilities.top1conf) * 100, 2)

    return predicted_class, confidence


def result(request):
    # This view can be left empty or used if you decide to separate logic further
    return render(request, 'result.html')
def signup(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('home')
    else:
        form = UserCreationForm()
    return render(request, 'signup.html', {'form': form})

# Login view
def login_view(request):
    if request.method == 'POST':
        username = request.POST['username']
        password = request.POST['password']
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect('home')
        else:
            messages.error(request, 'Invalid username or password.')
    return render(request, 'login.html')

# Logout view


def about(request):
    return render(request, 'about.html')

@login_required
def profile(request):
    return render(request, 'profile.html')
def edit_profile(request):
    if request.method == 'POST':
        form = UserChangeForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            return redirect('profile')
    else:
        form = UserChangeForm(instance=request.user)
    return render(request, 'edit_profile.html', {'form': form})

def logout_view(request):
    logout(request)
    return redirect('landing_page')
